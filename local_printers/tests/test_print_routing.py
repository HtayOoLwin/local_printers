import importlib
import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock


def _install_frappe_stub():
	try:
		import frappe  # noqa: F401
		return
	except ModuleNotFoundError:
		pass

	frappe = types.ModuleType("frappe")
	frappe._ = lambda value: value
	frappe.ValidationError = type("ValidationError", (Exception,), {})
	frappe.DuplicateEntryError = type("DuplicateEntryError", (Exception,), {})
	frappe.session = SimpleNamespace(user="test@example.com")
	frappe.db = SimpleNamespace()

	model = types.ModuleType("frappe.model")
	document = types.ModuleType("frappe.model.document")
	document.Document = type("Document", (), {})
	utils = types.ModuleType("frappe.utils")
	utils.cint = lambda value: int(value or 0)
	utils.now_datetime = lambda: None

	sys.modules["frappe"] = frappe
	sys.modules["frappe.model"] = model
	sys.modules["frappe.model.document"] = document
	sys.modules["frappe.utils"] = utils


_install_frappe_stub()

from local_printers import hooks

routing = importlib.import_module("local_printers.printing.routing")


class DictObject(dict):
	__getattr__ = dict.get


def row(name, item_code):
	return SimpleNamespace(name=name, item_code=item_code)


def config(
	name,
	printer,
	*,
	print_format=None,
	no_letterhead=0,
	is_default_kitchen=0,
):
	return DictObject(
		name=name,
		printer=printer,
		print_format=print_format,
		no_letterhead=no_letterhead,
		is_default_kitchen=is_default_kitchen,
	)


class RoutingTestCase(unittest.TestCase):
	def setUp(self):
		self.items = [
			row("SOI-1", "BURGER"),
			row("SOI-2", "FRIES"),
			row("SOI-3", "COLA"),
			row("SOI-4", "NAPKIN"),
		]
		self.doc = SimpleNamespace(
			doctype="Sales Order",
			name="SO-0001",
			pos_profile="Main POS",
			items=self.items,
		)
		self.frappish = SimpleNamespace(
			get_all=Mock(),
			get_print=Mock(return_value=b"pdf"),
			log_error=Mock(),
			publish_realtime=Mock(),
		)
		self.original_frappe = routing.frappe
		routing.frappe = self.frappish

	def tearDown(self):
		routing.frappe = self.original_frappe

	def set_query_results(self, configurations, groups, item_groups):
		def get_all(doctype, **kwargs):
			if doctype == "Printer Item Group":
				return configurations
			if doctype == "Printer Item Groups":
				return [DictObject(parent=parent, item_group=item_group) for parent, item_group in groups]
			if doctype == "Item":
				return [DictObject(name=name, item_group=item_group) for name, item_group in item_groups.items()]
			raise AssertionError(f"Unexpected query for {doctype}")

		self.frappish.get_all.side_effect = get_all

	def test_routes_explicit_groups_and_only_unmapped_rows_to_default(self):
		self.set_query_results(
			[
				config("CFG-GRILL", "Grill", print_format="Kitchen", no_letterhead=1),
				config("CFG-BAR", "Bar"),
				config("CFG-DEFAULT", "Expo", is_default_kitchen=1),
			],
			[("CFG-GRILL", "Food"), ("CFG-BAR", "Drinks")],
			{"BURGER": "Food", "FRIES": "Food", "COLA": "Drinks", "NAPKIN": "Supplies"},
		)

		routes = routing.route_order_items(self.doc, "on_submit")

		self.assertEqual(
			[(route.printer, route.source_rows) for route in routes],
			[("Grill", ("SOI-1", "SOI-2")), ("Bar", ("SOI-3",)), ("Expo", ("SOI-4",))],
		)
		self.assertEqual(routes[0].print_format, "Kitchen")
		self.assertTrue(routes[0].no_letterhead)
		self.assertTrue(all(route.ticket_type == "Kitchen" for route in routes))

	def test_item_groups_are_bulk_fetched_once_for_distinct_item_codes(self):
		self.doc.items.append(row("SOI-5", "BURGER"))
		self.set_query_results(
			[config("CFG-ALL", "Kitchen")],
			[("CFG-ALL", "All Item Groups")],
			{"BURGER": "Food", "FRIES": "Food", "COLA": "Drinks", "NAPKIN": "Supplies"},
		)

		routing.route_order_items(self.doc, "on_submit")

		item_calls = [entry for entry in self.frappish.get_all.call_args_list if entry.args[0] == "Item"]
		self.assertEqual(len(item_calls), 1)
		self.assertEqual(
			item_calls[0].kwargs["filters"],
			{"name": ["in", ["BURGER", "FRIES", "COLA", "NAPKIN"]]},
		)

	def test_all_item_groups_is_an_explicit_mapping(self):
		self.set_query_results(
			[
				config("CFG-ALL", "Kitchen"),
				config("CFG-DEFAULT", "Expo", is_default_kitchen=1),
			],
			[("CFG-ALL", "All Item Groups")],
			{"BURGER": "Food", "FRIES": "Food", "COLA": "Drinks", "NAPKIN": "Supplies"},
		)

		routes = routing.route_order_items(self.doc, "on_submit")

		self.assertEqual(len(routes), 1)
		self.assertEqual(routes[0].printer, "Kitchen")
		self.assertEqual(routes[0].source_rows, ("SOI-1", "SOI-2", "SOI-3", "SOI-4"))

	def test_missing_default_logs_unmapped_items_without_raising(self):
		self.set_query_results(
			[config("CFG-GRILL", "Grill")],
			[("CFG-GRILL", "Food")],
			{"BURGER": "Food", "FRIES": "Food", "COLA": "Drinks", "NAPKIN": "Supplies"},
		)

		routes = routing.route_order_items(self.doc, "on_submit")

		self.assertEqual([(route.printer, route.source_rows) for route in routes], [("Grill", ("SOI-1", "SOI-2"))])
		self.assertEqual(self.frappish.log_error.call_count, 1)
		message = self.frappish.log_error.call_args.args[0]
		self.assertIn("SO-0001", message)
		self.assertIn("COLA", message)
		self.assertIn("NAPKIN", message)

	def test_configuration_query_is_narrow_and_enabled(self):
		self.set_query_results([], [], {})

		routing.route_order_items(self.doc, "on_submit")

		printer_call = self.frappish.get_all.call_args_list[0]
		self.assertEqual(printer_call.args[0], "Printer Item Group")
		self.assertEqual(
			printer_call.kwargs["filters"],
			{
				"enabled": 1,
				"pos_profile": "Main POS",
				"target_doctype": "Sales Order",
				"trigger_method": "on_submit",
			},
		)


class EventJobTestCase(unittest.TestCase):
	def setUp(self):
		self.doc = SimpleNamespace(
			doctype="Sales Order",
			name="SO-0001",
			pos_profile="Main POS",
			items=[row("SOI-1", "BURGER"), row("SOI-2", "COLA")],
		)
		self.frappish = SimpleNamespace(
			get_all=Mock(),
			get_print=Mock(side_effect=lambda **kwargs: f"pdf:{','.join(item.name for item in kwargs['doc'].items)}".encode()),
			log_error=Mock(),
			publish_realtime=Mock(),
		)
		self.original_frappe = routing.frappe
		self.original_route = routing.route_order_items
		self.original_create = routing.create_print_job
		routing.frappe = self.frappish
		routing.create_print_job = Mock(
			side_effect=lambda **kwargs: SimpleNamespace(
				job_id=kwargs["event_key"],
				status="Pending",
				printer=kwargs["printer"],
				ticket_type=kwargs["ticket_type"],
			)
		)

	def tearDown(self):
		routing.frappe = self.original_frappe
		routing.route_order_items = self.original_route
		routing.create_print_job = self.original_create

	def test_submit_renders_copies_and_creates_deterministic_jobs(self):
		routing.route_order_items = Mock(
			return_value=[
				routing.PrinterRoute("CFG-A", "Grill", "Kitchen", True, "Kitchen", ("SOI-1",)),
				routing.PrinterRoute("CFG-B", "Bar", "Standard", False, "Kitchen", ("SOI-2",)),
			]
		)
		original_items = list(self.doc.items)

		routing.on_sales_order_submit(self.doc)

		self.assertEqual(self.doc.items, original_items)
		self.assertIsNot(self.frappish.get_print.call_args_list[0].kwargs["doc"], self.doc)
		self.assertEqual(
			[[item.name for item in entry.kwargs["doc"].items] for entry in self.frappish.get_print.call_args_list],
			[["SOI-1"], ["SOI-2"]],
		)
		self.assertEqual(
			[entry.kwargs["event_key"] for entry in routing.create_print_job.call_args_list],
			[
				"Sales Order/SO-0001/Grill/on_submit",
				"Sales Order/SO-0001/Bar/on_submit",
			],
		)
		self.assertEqual(routing.create_print_job.call_args_list[0].kwargs["source_rows"], ("SOI-1",))
		self.assertEqual(self.frappish.get_print.call_args_list[0].kwargs["pdf_options"], routing.ZERO_PDF_MARGINS)

	def test_submit_wake_event_has_metadata_but_no_payload(self):
		routing.route_order_items = Mock(
			return_value=[routing.PrinterRoute("CFG-A", "Grill", "Kitchen", True, "Kitchen", ("SOI-1",))]
		)

		routing.on_sales_order_submit(self.doc)

		self.frappish.publish_realtime.assert_called_once()
		wake = self.frappish.publish_realtime.call_args
		self.assertEqual(wake.kwargs["event"], "document_print_event")
		self.assertTrue(wake.kwargs["after_commit"])
		self.assertNotIn("payload", json.dumps(wake.kwargs["message"]).lower())
		self.assertEqual(wake.kwargs["message"]["document_name"], "SO-0001")

	def test_submit_notifies_successful_jobs_when_another_route_fails(self):
		routing.route_order_items = Mock(
			return_value=[
				routing.PrinterRoute("CFG-A", "Grill", "Kitchen", True, "Kitchen", ("SOI-1",)),
				routing.PrinterRoute("CFG-B", "Bar", "Standard", False, "Kitchen", ("SOI-2",)),
			]
		)
		routing.create_print_job.side_effect = [
			SimpleNamespace(job_id="job-grill", status="Pending", printer="Grill", ticket_type="Kitchen"),
			RuntimeError("Bar printer job failed"),
		]

		routing.on_sales_order_submit(self.doc)

		self.frappish.publish_realtime.assert_called_once()
		jobs = self.frappish.publish_realtime.call_args.kwargs["message"]["jobs"]
		self.assertEqual([job["job_id"] for job in jobs], ["job-grill"])
		self.assertEqual(self.frappish.log_error.call_count, 1)
		self.assertIn("Bar", self.frappish.log_error.call_args.args[1])

	def test_repeated_submit_has_one_durable_job_per_printer(self):
		routing.route_order_items = Mock(
			return_value=[
				routing.PrinterRoute("CFG-A", "Grill", "Kitchen", True, "Kitchen", ("SOI-1",)),
				routing.PrinterRoute("CFG-B", "Bar", "Standard", False, "Kitchen", ("SOI-2",)),
			]
		)
		persisted_jobs = {}

		def create_once(**kwargs):
			return persisted_jobs.setdefault(
				kwargs["event_key"],
				SimpleNamespace(
					job_id=kwargs["event_key"],
					status="Pending",
					printer=kwargs["printer"],
					ticket_type=kwargs["ticket_type"],
				),
			)

		routing.create_print_job.side_effect = create_once

		routing.on_sales_order_submit(self.doc)
		routing.on_sales_order_submit(self.doc)

		self.assertEqual(
			set(persisted_jobs),
			{
				"Sales Order/SO-0001/Grill/on_submit",
				"Sales Order/SO-0001/Bar/on_submit",
			},
		)
		self.assertEqual(len(persisted_jobs), 2)

	def test_cancel_uses_original_job_rows_without_item_rerouting(self):
		original_jobs = [
			DictObject(printer="Grill", print_format="Kitchen", no_letterhead=0, source_rows='["SOI-1"]'),
			DictObject(printer="Bar", print_format="Bar Ticket", no_letterhead=1, source_rows='["SOI-2"]'),
		]
		cancel_configs = [config("CFG-CANCEL", "Grill", print_format="Cancel Kitchen", no_letterhead=1)]

		def get_all(doctype, **kwargs):
			if doctype == "Local Print Job":
				return original_jobs
			if doctype == "Printer Item Group":
				return cancel_configs
			raise AssertionError(f"Cancellation must not query {doctype}")

		self.frappish.get_all.side_effect = get_all
		routing.route_order_items = Mock(side_effect=AssertionError("must not reroute cancellation"))

		routing.on_sales_order_cancel(self.doc)

		routing.route_order_items.assert_not_called()
		self.assertEqual(
			[entry.kwargs["print_format"] for entry in routing.create_print_job.call_args_list],
			["Cancel Kitchen", "Bar Ticket"],
		)
		self.assertEqual(
			[entry.kwargs["event_key"] for entry in routing.create_print_job.call_args_list],
			[
				"Sales Order/SO-0001/Grill/on_cancel",
				"Sales Order/SO-0001/Bar/on_cancel",
			],
		)
		self.assertTrue(all(entry.kwargs["ticket_type"] == "Cancel" for entry in routing.create_print_job.call_args_list))
		self.assertEqual(
			[[item.name for item in entry.kwargs["doc"].items] for entry in self.frappish.get_print.call_args_list],
			[["SOI-1"], ["SOI-2"]],
		)
		self.assertEqual(
			[entry.kwargs["no_letterhead"] for entry in self.frappish.get_print.call_args_list],
			[True, True],
		)

	def test_cancel_notifies_successful_jobs_when_another_route_fails(self):
		self.frappish.get_all.side_effect = lambda doctype, **kwargs: (
			[
				DictObject(printer="Grill", print_format="Kitchen", no_letterhead=0, source_rows='["SOI-1"]'),
				DictObject(printer="Bar", print_format="Bar Ticket", no_letterhead=0, source_rows='["SOI-2"]'),
			]
			if doctype == "Local Print Job"
			else []
		)
		routing.create_print_job.side_effect = [
			SimpleNamespace(job_id="job-grill", status="Pending", printer="Grill", ticket_type="Cancel"),
			RuntimeError("Bar cancel job failed"),
		]

		routing.on_sales_order_cancel(self.doc)

		self.frappish.publish_realtime.assert_called_once()
		jobs = self.frappish.publish_realtime.call_args.kwargs["message"]["jobs"]
		self.assertEqual([job["job_id"] for job in jobs], ["job-grill"])
		self.assertEqual(self.frappish.log_error.call_count, 1)
		self.assertIn("Bar", self.frappish.log_error.call_args.args[1])

	def test_cancel_uses_later_valid_route_for_same_printer(self):
		self.frappish.get_all.side_effect = lambda doctype, **kwargs: (
			[
				DictObject(printer="Grill", print_format="Old", no_letterhead=0, source_rows=None),
				DictObject(printer="Grill", print_format="Kitchen", no_letterhead=1, source_rows='["SOI-1"]'),
			]
			if doctype == "Local Print Job"
			else []
		)

		routing.on_sales_order_cancel(self.doc)

		routing.create_print_job.assert_called_once()
		self.assertEqual(routing.create_print_job.call_args.kwargs["source_rows"], ("SOI-1",))
		self.assertTrue(self.frappish.get_print.call_args.kwargs["no_letterhead"])


class HookTestCase(unittest.TestCase):
	def test_only_sales_order_submit_and_cancel_are_automatic(self):
		self.assertEqual(
			hooks.doc_events,
			{
				"Sales Order": {
					"on_submit": "local_printers.printing.routing.on_sales_order_submit",
					"on_cancel": "local_printers.printing.routing.on_sales_order_cancel",
				}
			},
		)


if __name__ == "__main__":
	unittest.main()
