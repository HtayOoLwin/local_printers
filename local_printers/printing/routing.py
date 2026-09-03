import copy
import json
from dataclasses import dataclass

import frappe

from local_printers.printing.jobs import create_print_job


ZERO_PDF_MARGINS = {
	"margin-left": "0mm",
	"margin-right": "0mm",
	"margin-top": "0mm",
	"margin-bottom": "0mm",
}


@dataclass(frozen=True)
class PrinterRoute:
	configuration_name: str
	printer: str
	print_format: str
	no_letterhead: bool
	ticket_type: str
	source_rows: tuple[str, ...]


def route_order_items(doc, trigger_method: str) -> list[PrinterRoute]:
	"""Build one ordered route per destination printer for a Sales Order."""
	if not getattr(doc, "pos_profile", None):
		return []

	configurations = frappe.get_all(
		"Printer Item Group",
		filters={
			"enabled": 1,
			"pos_profile": doc.pos_profile,
			"target_doctype": "Sales Order",
			"trigger_method": trigger_method,
		},
		fields=[
			"name",
			"printer",
			"print_format",
			"no_letterhead",
			"is_default_kitchen",
		],
		order_by="name asc",
	)
	items = list(getattr(doc, "items", None) or [])
	if not items:
		return []

	configuration_names = [_value(configuration, "name") for configuration in configurations]
	group_rows = (
		frappe.get_all(
			"Printer Item Groups",
			filters={"parent": ["in", configuration_names]},
			fields=["parent", "item_group"],
		)
		if configuration_names
		else []
	)
	groups_by_configuration = {name: set() for name in configuration_names}
	for group_row in group_rows:
		groups_by_configuration.setdefault(_value(group_row, "parent"), set()).add(
			_value(group_row, "item_group")
		)

	item_codes = list(dict.fromkeys(row.item_code for row in items if row.item_code))
	item_group_rows = (
		frappe.get_all(
			"Item",
			filters={"name": ["in", item_codes]},
			fields=["name", "item_group"],
		)
		if item_codes
		else []
	)
	item_groups = {
		_value(item_group_row, "name"): _value(item_group_row, "item_group")
		for item_group_row in item_group_rows
	}

	default_configuration = next(
		(
			configuration
			for configuration in configurations
			if _as_bool(_value(configuration, "is_default_kitchen"))
		),
		None,
	)
	rows_by_printer: dict[str, list[str]] = {}
	configuration_by_printer = {}
	unmapped_items = []

	for item in items:
		item_group = item_groups.get(item.item_code)
		explicit_printers = set()
		for configuration in configurations:
			configuration_name = _value(configuration, "name")
			configured_groups = groups_by_configuration.get(configuration_name, set())
			if item_group not in configured_groups and "All Item Groups" not in configured_groups:
				continue

			printer = _value(configuration, "printer")
			if not printer or printer in explicit_printers:
				continue
			explicit_printers.add(printer)
			configuration_by_printer.setdefault(printer, configuration)
			rows_by_printer.setdefault(printer, []).append(item.name)

		if explicit_printers:
			continue
		if default_configuration:
			printer = _value(default_configuration, "printer")
			if printer:
				configuration_by_printer.setdefault(printer, default_configuration)
				rows_by_printer.setdefault(printer, []).append(item.name)
				continue
		unmapped_items.append(item.item_code)

	if unmapped_items:
		frappe.log_error(
			f"Sales Order {doc.name} has unmapped kitchen items and no enabled "
			f"default kitchen printer: {', '.join(unmapped_items)}",
			"Unmapped Sales Order kitchen items",
		)

	ticket_type = "Cancel" if trigger_method == "on_cancel" else "Kitchen"
	return [
		PrinterRoute(
			configuration_name=_value(configuration_by_printer[printer], "name"),
			printer=printer,
			print_format=_value(configuration_by_printer[printer], "print_format") or "Standard",
			no_letterhead=_as_bool(
				_value(configuration_by_printer[printer], "no_letterhead")
			),
			ticket_type=ticket_type,
			source_rows=tuple(source_rows),
		)
		for printer, source_rows in rows_by_printer.items()
		if source_rows
	]


def on_sales_order_submit(doc, method=None) -> None:
	try:
		jobs = [
			_create_route_job(doc, route, "on_submit")
			for route in route_order_items(doc, "on_submit")
		]
		_publish_wake_notification(doc, "on_submit", jobs)
	except Exception:
		_log_handler_error(doc, "on_submit")


def on_sales_order_cancel(doc, method=None) -> None:
	try:
		original_jobs = frappe.get_all(
			"Local Print Job",
			filters={
				"source_doctype": "Sales Order",
				"source_name": doc.name,
				"ticket_type": "Kitchen",
			},
			fields=["printer", "print_format", "source_rows"],
			order_by="creation asc, name asc",
		)
		if not original_jobs:
			return

		cancel_configurations = frappe.get_all(
			"Printer Item Group",
			filters={
				"enabled": 1,
				"pos_profile": doc.pos_profile,
				"target_doctype": "Sales Order",
				"trigger_method": "on_cancel",
			},
			fields=["name", "printer", "print_format", "no_letterhead"],
			order_by="name asc",
		)
		cancel_by_printer = {
			_value(configuration, "printer"): configuration
			for configuration in cancel_configurations
		}

		jobs = []
		seen_printers = set()
		for original_job in original_jobs:
			printer = _value(original_job, "printer")
			if not printer or printer in seen_printers:
				continue
			seen_printers.add(printer)
			source_rows = _decode_source_rows(_value(original_job, "source_rows"))
			if not source_rows:
				frappe.log_error(
					f"Sales Order {doc.name} has no durable route rows for printer {printer}; "
					"the Cancel ticket was skipped.",
					"Missing Sales Order cancel route metadata",
				)
				continue

			cancel_configuration = cancel_by_printer.get(printer)
			route = PrinterRoute(
				configuration_name=(
					_value(cancel_configuration, "name") if cancel_configuration else ""
				),
				printer=printer,
				print_format=(
					_value(cancel_configuration, "print_format")
					if cancel_configuration
					else _value(original_job, "print_format")
				)
				or "Standard",
				no_letterhead=(
					_as_bool(_value(cancel_configuration, "no_letterhead"))
					if cancel_configuration
					else False
				),
				ticket_type="Cancel",
				source_rows=source_rows,
			)
			jobs.append(_create_route_job(doc, route, "on_cancel"))

		_publish_wake_notification(doc, "on_cancel", jobs)
	except Exception:
		_log_handler_error(doc, "on_cancel")


def _create_route_job(doc, route: PrinterRoute, trigger_method: str):
	render_doc = copy.deepcopy(doc)
	route_rows = set(route.source_rows)
	render_doc.items = [row for row in render_doc.items if row.name in route_rows]
	payload = frappe.get_print(
		doctype="Sales Order",
		name=doc.name,
		print_format=route.print_format,
		no_letterhead=route.no_letterhead,
		doc=render_doc,
		as_pdf=True,
		pdf_options=ZERO_PDF_MARGINS,
	)
	return create_print_job(
		source_doc=doc,
		printer=route.printer,
		ticket_type=route.ticket_type,
		print_format=route.print_format,
		payload=payload,
		event_key=f"Sales Order/{doc.name}/{route.printer}/{trigger_method}",
		source_rows=route.source_rows,
	)


def _publish_wake_notification(doc, trigger_method: str, jobs) -> None:
	if not jobs:
		return
	frappe.publish_realtime(
		event="document_print_event",
		message={
			"doctype": "Sales Order",
			"document_name": doc.name,
			"method": trigger_method,
			"jobs": [
				{
					"job_id": _value(job, "job_id"),
					"status": _value(job, "status"),
					"printer": _value(job, "printer"),
					"ticket_type": _value(job, "ticket_type"),
				}
				for job in jobs
			],
		},
		after_commit=True,
	)


def _decode_source_rows(value) -> tuple[str, ...]:
	if not value:
		return ()
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except (TypeError, ValueError):
			return ()
	if not isinstance(value, (list, tuple)):
		return ()
	return tuple(row_name for row_name in value if isinstance(row_name, str) and row_name)


def _as_bool(value) -> bool:
	if isinstance(value, str):
		return value not in ("", "0", "false", "False")
	return bool(value)


def _value(record, fieldname):
	if isinstance(record, dict):
		return record.get(fieldname)
	return getattr(record, fieldname, None)


def _log_handler_error(doc, trigger_method: str) -> None:
	traceback = (
		frappe.get_traceback()
		if hasattr(frappe, "get_traceback")
		else f"Unable to create {trigger_method} print jobs."
	)
	frappe.log_error(
		traceback,
		f"Error creating {trigger_method} print jobs for Sales Order {doc.name}",
	)
