import base64
import uuid

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, now_datetime


MAX_AUTOMATIC_ATTEMPTS = 3

CLAIMED_JOB_FIELDS = (
	"job_id",
	"event_key",
	"source_doctype",
	"source_name",
	"pos_profile",
	"ticket_type",
	"printer",
	"print_format",
	"status",
	"attempt_count",
	"worker_id",
	"claimed_at",
	"payload",
)


def create_print_job(
	*,
	source_doc,
	printer: str,
	ticket_type: str,
	print_format: str | None,
	payload: bytes | str,
	event_key: str | None = None,
) -> Document:
	"""Persist a print payload, deduplicating deterministic ticket events."""
	if ticket_type not in ("Kitchen", "Cancel", "Cashier"):
		frappe.throw(_("Unsupported ticket type."), frappe.ValidationError)

	event_key = event_key or None
	if ticket_type in ("Kitchen", "Cancel") and not event_key:
		frappe.throw(
			_("An event key is required for Kitchen and Cancel tickets."),
			frappe.ValidationError,
		)
	if ticket_type == "Cashier" and event_key:
		frappe.throw(
			_("Cashier tickets cannot use a deterministic event key."),
			frappe.ValidationError,
		)

	if event_key:
		existing_name = frappe.db.get_value(
			"Local Print Job", {"event_key": event_key}, "name"
		)
		if existing_name:
			return frappe.get_doc("Local Print Job", existing_name)

	if isinstance(payload, bytes):
		encoded_payload = base64.b64encode(payload).decode("ascii")
	elif isinstance(payload, str):
		encoded_payload = payload
	else:
		raise TypeError("payload must be bytes or a base64 string")

	job = frappe.get_doc(
		{
			"doctype": "Local Print Job",
			"job_id": str(uuid.uuid4()),
			"event_key": event_key,
			"source_doctype": source_doc.doctype,
			"source_name": source_doc.name,
			"pos_profile": source_doc.pos_profile,
			"ticket_type": ticket_type,
			"printer": printer,
			"print_format": print_format,
			"status": "Pending",
			"attempt_count": 0,
			"payload": encoded_payload,
			"requested_by": frappe.session.user,
		}
	)
	try:
		job.insert(ignore_permissions=True, ignore_links=True)
	except frappe.DuplicateEntryError:
		if event_key:
			existing_name = frappe.db.get_value(
				"Local Print Job", {"event_key": event_key}, "name"
			)
			if existing_name:
				return frappe.get_doc("Local Print Job", existing_name)
		raise

	return job


def claim_next_jobs(worker_id: str, limit: int = 10) -> list[dict]:
	"""Lock and claim pending jobs in the caller's database transaction."""
	if not worker_id:
		frappe.throw(_("Worker ID is required."), frappe.ValidationError)

	limit = cint(limit)
	if limit <= 0:
		return []

	# SKIP LOCKED ensures concurrent workers select disjoint rows. These row locks
	# remain held until Frappe commits or rolls back the surrounding request.
	rows = frappe.db.sql(
		"""
		SELECT name
		FROM `tabLocal Print Job`
		WHERE status = 'Pending'
			AND attempt_count < %(attempt_limit)s
		ORDER BY creation ASC, name ASC
		LIMIT %(limit)s
		FOR UPDATE SKIP LOCKED
		""",
		{"attempt_limit": MAX_AUTOMATIC_ATTEMPTS, "limit": limit},
		as_dict=True,
	)
	if not rows:
		return []

	claimed_at = now_datetime()
	for row in rows:
		frappe.db.set_value(
			"Local Print Job",
			row.name,
			{
				"status": "Printing",
				"worker_id": worker_id,
				"claimed_at": claimed_at,
				"attempt_count": cint(
					frappe.db.get_value(
						"Local Print Job", row.name, "attempt_count"
					)
				)
				+ 1,
			},
			update_modified=False,
		)

	return [
		dict(
			frappe.db.get_value(
				"Local Print Job",
				row.name,
				CLAIMED_JOB_FIELDS,
				as_dict=True,
			)
		)
		for row in rows
	]


def acknowledge_job(
	job_id: str,
	worker_id: str,
	success: bool,
	error: str | None = None,
) -> dict:
	"""Record a worker result while holding the job row lock."""
	rows = frappe.db.sql(
		"""
		SELECT name, job_id, status, worker_id, attempt_count
		FROM `tabLocal Print Job`
		WHERE job_id = %(job_id)s
		FOR UPDATE
		""",
		{"job_id": job_id},
		as_dict=True,
	)
	if not rows:
		frappe.throw(_("Unknown local print job."), frappe.ValidationError)

	job = rows[0]
	if job.status != "Printing":
		frappe.throw(
			_("Only a Printing job can be acknowledged."),
			frappe.ValidationError,
		)
	if job.worker_id != worker_id:
		frappe.throw(
			_("This print job is claimed by a different worker."),
			frappe.ValidationError,
		)

	if success:
		values = {
			"status": "Success",
			"printed_at": now_datetime(),
			"error_message": None,
		}
	else:
		values = {
			"status": (
				"Pending"
				if cint(job.attempt_count) < MAX_AUTOMATIC_ATTEMPTS
				else "Failed"
			),
			"worker_id": None,
			"claimed_at": None,
			"error_message": error,
		}

	frappe.db.set_value(
		"Local Print Job",
		job.name,
		values,
		update_modified=False,
	)
	return dict(
		frappe.db.get_value(
			"Local Print Job",
			job.name,
			(
				"job_id",
				"status",
				"attempt_count",
				"worker_id",
				"claimed_at",
				"printed_at",
				"error_message",
			),
			as_dict=True,
		)
	)
