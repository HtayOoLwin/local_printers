# Copyright (c) 2026, mohammed hassan and Contributors
# See license.txt

import base64
import json

import frappe
from frappe.tests.utils import FrappeTestCase

from local_printers.printing.jobs import (
	MAX_AUTOMATIC_ATTEMPTS,
	acknowledge_job,
	claim_next_jobs,
	create_print_job,
)


class TestLocalPrintJob(FrappeTestCase):
	def setUp(self):
		super().setUp()
		self.source_doc = frappe._dict(
			doctype="Sales Order",
			name=f"TEST-SO-{frappe.generate_hash(length=10)}",
			pos_profile=f"Test POS Profile {frappe.generate_hash(length=8)}",
		)
		self.printer = f"Test Printer {frappe.generate_hash(length=8)}"

	def _create_job(self, *, ticket_type="Kitchen", event_key=None):
		if event_key is None and ticket_type in ("Kitchen", "Cancel"):
			event_key = f"test-event-{frappe.generate_hash(length=12)}"

		return create_print_job(
			source_doc=self.source_doc,
			printer=self.printer,
			ticket_type=ticket_type,
			print_format="Standard",
			payload=b"test-pdf",
			event_key=event_key,
		)

	def _claimed_job(self, worker_id="worker-a"):
		job = self._create_job()
		claimed = claim_next_jobs(worker_id, limit=100)
		self.assertIn(job.job_id, {row["job_id"] for row in claimed})
		return frappe.get_doc("Local Print Job", job.name)

	def test_deterministic_event_returns_existing_job(self):
		event_key = f"kitchen-{frappe.generate_hash(length=12)}"

		first = self._create_job(event_key=event_key)
		second = self._create_job(event_key=event_key)

		self.assertEqual(second.name, first.name)
		self.assertEqual(
			frappe.db.count("Local Print Job", {"event_key": event_key}),
			1,
		)

	def test_cashier_events_create_unique_jobs(self):
		first = self._create_job(ticket_type="Cashier")
		second = self._create_job(ticket_type="Cashier")

		self.assertNotEqual(second.job_id, first.job_id)
		self.assertIsNone(first.event_key)
		self.assertIsNone(second.event_key)

	def test_deterministic_ticket_types_require_event_keys(self):
		for ticket_type in ("Kitchen", "Cancel"):
			with self.subTest(ticket_type=ticket_type), self.assertRaises(
				frappe.ValidationError
			):
				create_print_job(
					source_doc=self.source_doc,
					printer=self.printer,
					ticket_type=ticket_type,
					print_format=None,
					payload=b"test-pdf",
				)

	def test_cashier_ticket_rejects_deterministic_event_key(self):
		with self.assertRaises(frappe.ValidationError):
			self._create_job(
				ticket_type="Cashier",
				event_key=f"cashier-{frappe.generate_hash(length=12)}",
			)

	def test_bytes_payload_is_stored_as_base64(self):
		job = self._create_job()

		self.assertEqual(job.payload, base64.b64encode(b"test-pdf").decode("ascii"))

	def test_string_payload_is_stored_without_reencoding(self):
		encoded_payload = base64.b64encode(b"already-encoded").decode("ascii")

		job = create_print_job(
			source_doc=self.source_doc,
			printer=self.printer,
			ticket_type="Kitchen",
			print_format=None,
			payload=encoded_payload,
			event_key=f"test-event-{frappe.generate_hash(length=12)}",
		)

		self.assertEqual(job.payload, encoded_payload)

	def test_source_rows_are_stored_as_json_route_metadata(self):
		job = create_print_job(
			source_doc=self.source_doc,
			printer=self.printer,
			ticket_type="Kitchen",
			print_format="Standard",
			payload=b"test-pdf",
			event_key=f"test-event-{frappe.generate_hash(length=12)}",
			source_rows=("SOI-1", "SOI-2"),
		)

		self.assertEqual(json.loads(job.source_rows), ["SOI-1", "SOI-2"])

	def test_claim_transitions_job_and_increments_attempt(self):
		job = self._create_job()

		claimed = claim_next_jobs("worker-a", limit=100)
		claimed_by_id = {row["job_id"]: row for row in claimed}
		claimed_job = frappe.get_doc("Local Print Job", job.name)

		self.assertEqual(claimed_job.status, "Printing")
		self.assertEqual(claimed_job.attempt_count, 1)
		self.assertEqual(claimed_job.worker_id, "worker-a")
		self.assertIsNotNone(claimed_job.claimed_at)
		self.assertEqual(claimed_by_id[job.job_id]["payload"], job.payload)

	def test_acknowledgement_rejects_different_worker(self):
		job = self._claimed_job("worker-a")

		with self.assertRaises(frappe.ValidationError):
			acknowledge_job(job.job_id, "worker-b", success=True)

	def test_acknowledgement_rejects_unknown_job(self):
		with self.assertRaises(frappe.ValidationError):
			acknowledge_job("missing-job", "worker-a", success=True)

	def test_acknowledgement_rejects_pending_job(self):
		job = self._create_job()

		with self.assertRaises(frappe.ValidationError):
			acknowledge_job(job.job_id, "worker-a", success=True)

	def test_success_acknowledgement_marks_job_printed(self):
		job = self._claimed_job("worker-a")

		result = acknowledge_job(job.job_id, "worker-a", success=True)
		updated = frappe.get_doc("Local Print Job", job.name)

		self.assertEqual(result["status"], "Success")
		self.assertEqual(updated.status, "Success")
		self.assertIsNotNone(updated.printed_at)
		self.assertFalse(updated.error_message)

	def test_failure_before_attempt_limit_requeues_job(self):
		job = self._claimed_job("worker-a")

		result = acknowledge_job(
			job.job_id,
			"worker-a",
			success=False,
			error="printer offline",
		)
		updated = frappe.get_doc("Local Print Job", job.name)

		self.assertEqual(result["status"], "Pending")
		self.assertEqual(updated.status, "Pending")
		self.assertEqual(updated.error_message, "printer offline")
		self.assertFalse(updated.worker_id)
		self.assertIsNone(updated.claimed_at)

	def test_failure_at_attempt_limit_marks_job_failed(self):
		job = self._claimed_job("worker-a")
		frappe.db.set_value(
			"Local Print Job",
			job.name,
			"attempt_count",
			MAX_AUTOMATIC_ATTEMPTS,
			update_modified=False,
		)

		result = acknowledge_job(
			job.job_id,
			"worker-a",
			success=False,
			error="paper jam",
		)
		updated = frappe.get_doc("Local Print Job", job.name)

		self.assertEqual(result["status"], "Failed")
		self.assertEqual(updated.status, "Failed")
		self.assertEqual(updated.error_message, "paper jam")
		self.assertFalse(updated.worker_id)
		self.assertIsNone(updated.claimed_at)

	def test_claim_skips_jobs_at_attempt_limit(self):
		job = self._create_job()
		frappe.db.set_value(
			"Local Print Job",
			job.name,
			"attempt_count",
			MAX_AUTOMATIC_ATTEMPTS,
			update_modified=False,
		)

		claimed = claim_next_jobs("worker-a", limit=100)

		self.assertNotIn(job.job_id, {row["job_id"] for row in claimed})
		self.assertEqual(
			frappe.db.get_value("Local Print Job", job.name, "status"),
			"Pending",
		)
