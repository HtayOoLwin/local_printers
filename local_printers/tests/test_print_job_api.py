import importlib
import re
import sys
import types
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace


NOW = datetime(2026, 9, 3, 12, 0, 0)
MISSING = object()


class DictObject(dict):
	__getattr__ = dict.get


def _install_frappe_stub():
	frappe = sys.modules.get("frappe")
	if frappe is not None and hasattr(frappe, "get_site_path"):
		return frappe

	frappe = frappe or types.ModuleType("frappe")
	frappe._ = lambda value: value
	frappe.ValidationError = type("ValidationError", (Exception,), {})
	frappe.PermissionError = type("PermissionError", (Exception,), {})
	frappe.AuthenticationError = type("AuthenticationError", (Exception,), {})
	frappe.DuplicateEntryError = type("DuplicateEntryError", (Exception,), {})
	frappe.session = SimpleNamespace(user="worker@example.com")
	frappe.allowed_http_methods_for_whitelisted_func = {}

	def whitelist(*, allow_guest=False, methods=None, **kwargs):
		allowed_methods = list(methods or ("GET", "POST", "PUT", "DELETE"))

		def decorate(function):
			frappe.allowed_http_methods_for_whitelisted_func[function] = allowed_methods
			return function

		return decorate

	frappe.whitelist = whitelist
	frappe.throw = lambda message, exc=None, **kwargs: (_ for _ in ()).throw(
		(exc or frappe.ValidationError)(message)
	)
	frappe.get_roles = lambda user=None: list(getattr(frappe, "_test_roles", []))
	frappe.has_permission = lambda *args, **kwargs: getattr(
		frappe, "_test_has_permission", True
	)

	model = sys.modules.get("frappe.model") or types.ModuleType("frappe.model")
	document = sys.modules.get("frappe.model.document") or types.ModuleType(
		"frappe.model.document"
	)
	document.Document = getattr(document, "Document", type("Document", (), {}))
	utils = sys.modules.get("frappe.utils") or types.ModuleType("frappe.utils")
	utils.cint = lambda value: int(value or 0)
	utils.get_datetime = lambda value=None: value or NOW
	utils.now_datetime = lambda: NOW

	sys.modules["frappe"] = frappe
	sys.modules["frappe.model"] = model
	sys.modules["frappe.model.document"] = document
	sys.modules["frappe.utils"] = utils
	return frappe


frappe = _install_frappe_stub()


class FakeDocument(DictObject):
	def __init__(self, db, values):
		super().__init__(values)
		self.db = db

	def insert(self, **kwargs):
		if self.doctype != "Local Printer Worker Heartbeat":
			raise AssertionError(f"Unexpected insert for {self.doctype}")
		if self.worker_id in self.db.heartbeats:
			raise frappe.DuplicateEntryError(self.worker_id)
		self.db.heartbeats[self.worker_id] = dict(self)
		return self


class FakeDB:
	def __init__(self):
		self.jobs = {}
		self.heartbeats = {}
		self.pos_profiles = {"Main POS", "Other POS"}
		self.last_claim_limit = None
		self.job_snapshot_attempt_counts = {}
		self.heartbeat_snapshot_stale = False
		self.heartbeat_current_reads = []

	def add_job(
		self,
		job_id,
		*,
		pos_profile="Main POS",
		status="Pending",
		attempt_count=0,
		worker_id=None,
	):
		self.jobs[job_id] = {
			"name": job_id,
			"job_id": job_id,
			"event_key": None,
			"source_doctype": "Sales Order",
			"source_name": "SO-0001",
			"pos_profile": pos_profile,
			"ticket_type": "Kitchen",
			"printer": "Kitchen",
			"print_format": "Kitchen",
			"no_letterhead": 1,
			"source_rows": '["SOI-1"]',
			"status": status,
			"attempt_count": attempt_count,
			"worker_id": worker_id,
			"claimed_at": NOW if worker_id else None,
			"printed_at": None,
			"error_message": "paper jam" if status == "Failed" else None,
			"payload": "cGRm",
			"requested_by": "order@example.com",
			"creation": NOW,
		}

	def sql(self, query, values, as_dict=False):
		if "FROM `tabLocal Printer Worker Heartbeat`" in query:
			if "WHERE name = %(worker_id)s" not in query or "FOR UPDATE" not in query:
				raise AssertionError("Heartbeat recovery must use a bound locking read")
			if values != {"worker_id": values.get("worker_id")}:
				raise AssertionError("Heartbeat recovery accepts only worker_id")
			if values["worker_id"] in query:
				raise AssertionError("Worker ID must be passed as a SQL parameter")
			self.heartbeat_current_reads.append(dict(values))
			heartbeat = self.heartbeats.get(values["worker_id"])
			return [DictObject(user=heartbeat["user"])] if heartbeat else []

		if "WHERE status = 'Pending'" in query:
			self.last_claim_limit = values["limit"]
			rows = [
				job
				for job in self.jobs.values()
				if job["status"] == "Pending"
				and job["attempt_count"] < values["attempt_limit"]
			]
			rows.sort(key=lambda job: (job["creation"], job["name"]))
			return [
				DictObject(name=job["name"], attempt_count=job["attempt_count"])
				for job in rows[: values["limit"]]
			]

		if "WHERE job_id = %(job_id)s" in query:
			job = self.jobs.get(values["job_id"])
			return [DictObject(job)] if job else []

		raise AssertionError(f"Unexpected SQL: {re.sub(r'\\s+', ' ', query).strip()}")

	def get_value(self, doctype, name_or_filters, fields, as_dict=False):
		if doctype == "Local Print Job":
			if isinstance(name_or_filters, dict):
				job = next(
					(
						row
						for row in self.jobs.values()
						if all(row.get(key) == value for key, value in name_or_filters.items())
					),
					None,
				)
			else:
				job = self.jobs.get(name_or_filters)
			if not job:
				return None
			if (
				fields == "attempt_count"
				and not isinstance(name_or_filters, dict)
				and name_or_filters in self.job_snapshot_attempt_counts
			):
				return self.job_snapshot_attempt_counts[name_or_filters]
			if isinstance(fields, (tuple, list)):
				result = DictObject({field: job.get(field) for field in fields})
				return result if as_dict else tuple(result.values())
			return job.get(fields)
		if doctype == "Local Printer Worker Heartbeat":
			if self.heartbeat_snapshot_stale:
				return None
			heartbeat = self.heartbeats.get(name_or_filters)
			return heartbeat.get(fields) if heartbeat else None

		raise AssertionError(f"Unexpected get_value for {doctype}")

	def set_value(
		self,
		doctype,
		name,
		field_or_values,
		value=None,
		update_modified=True,
	):
		if doctype == "Local Print Job":
			record = self.jobs[name]
		elif doctype == "Local Printer Worker Heartbeat":
			record = self.heartbeats[name]
		else:
			raise AssertionError(f"Unexpected set_value for {doctype}")

		if isinstance(field_or_values, dict):
			record.update(field_or_values)
		else:
			record[field_or_values] = value
		return name

	def exists(self, doctype, name_or_filters):
		if doctype == "Local Printer Worker Heartbeat":
			return name_or_filters if name_or_filters in self.heartbeats else None
		if doctype == "POS Profile":
			return name_or_filters if name_or_filters in self.pos_profiles else None
		raise AssertionError(f"Unexpected exists for {doctype}")

	def count(self, doctype, filters):
		if doctype != "Local Print Job":
			raise AssertionError(f"Unexpected count for {doctype}")
		return sum(
			all(job.get(key) == value for key, value in filters.items())
			for job in self.jobs.values()
		)


jobs = importlib.import_module("local_printers.printing.jobs")
print_jobs = importlib.import_module("local_printers.api.print_jobs")


class PrintJobAPITestCase(unittest.TestCase):
	JOB_A = "11111111-1111-4111-8111-111111111111"
	JOB_B = "22222222-2222-4222-8222-222222222222"
	JOB_C = "33333333-3333-4333-8333-333333333333"
	JOB_WITH_HEX = "abcdef12-abcd-4abc-8abc-abcdefabcdef"

	def setUp(self):
		self.db = FakeDB()
		self.originals = {
			"db": getattr(frappe, "db", MISSING),
			"get_doc": getattr(frappe, "get_doc", MISSING),
			"get_all": getattr(frappe, "get_all", MISSING),
			"get_roles": getattr(frappe, "get_roles", MISSING),
			"has_permission": getattr(frappe, "has_permission", MISSING),
			"_test_roles": getattr(frappe, "_test_roles", MISSING),
			"_test_has_permission": getattr(
				frappe, "_test_has_permission", MISSING
			),
			"user": frappe.session.user,
			"jobs_frappe": jobs.frappe,
			"jobs_now_datetime": jobs.now_datetime,
			"api_frappe": print_jobs.frappe,
			"api_now_datetime": print_jobs.now_datetime,
		}
		frappe.db = self.db
		frappe.session.user = "worker@example.com"
		frappe._test_roles = ["Local Printer Worker"]
		frappe._test_has_permission = True
		frappe.get_roles = lambda user=None: list(frappe._test_roles)
		frappe.has_permission = lambda *args, **kwargs: frappe._test_has_permission
		frappe.get_doc = lambda values: FakeDocument(self.db, values)
		frappe.get_all = self._get_all
		jobs.frappe = frappe
		jobs.now_datetime = lambda: NOW
		print_jobs.frappe = frappe
		print_jobs.now_datetime = lambda: NOW

	def tearDown(self):
		for attribute in (
			"db",
			"get_doc",
			"get_all",
			"get_roles",
			"has_permission",
			"_test_roles",
			"_test_has_permission",
		):
			original = self.originals[attribute]
			if original is MISSING:
				delattr(frappe, attribute)
			else:
				setattr(frappe, attribute, original)
		frappe.session.user = self.originals["user"]
		jobs.frappe = self.originals["jobs_frappe"]
		jobs.now_datetime = self.originals["jobs_now_datetime"]
		print_jobs.frappe = self.originals["api_frappe"]
		print_jobs.now_datetime = self.originals["api_now_datetime"]

	def _get_all(self, doctype, **kwargs):
		if doctype != "Local Printer Worker Heartbeat":
			raise AssertionError(f"Unexpected get_all for {doctype}")
		rows = sorted(
			self.db.heartbeats.values(),
			key=lambda row: row["last_seen"],
			reverse=True,
		)
		return [DictObject(row) for row in rows[: kwargs.get("limit", len(rows))]]

	def bind_worker(self, worker_id="kitchen-worker-1", user="worker@example.com"):
		self.db.heartbeats[worker_id] = {
			"doctype": "Local Printer Worker Heartbeat",
			"worker_id": worker_id,
			"user": user,
			"last_seen": NOW - timedelta(minutes=1),
		}

	def test_mutating_endpoints_are_post_only_and_status_allows_get(self):
		methods = frappe.allowed_http_methods_for_whitelisted_func

		self.assertEqual(methods[print_jobs.claim_jobs], ["POST"])
		self.assertEqual(methods[print_jobs.acknowledge], ["POST"])
		self.assertEqual(methods[print_jobs.retry_failed], ["POST"])
		self.assertIn("GET", methods[print_jobs.get_status])

	def test_worker_claims_jobs_through_lifecycle_service_and_updates_heartbeat(self):
		self.db.add_job(self.JOB_A)

		result = print_jobs.claim_jobs("kitchen-worker-1", limit=1)

		self.assertEqual([job["job_id"] for job in result["jobs"]], [self.JOB_A])
		self.assertEqual(self.db.jobs[self.JOB_A]["status"], "Printing")
		self.assertEqual(self.db.jobs[self.JOB_A]["attempt_count"], 1)
		self.assertEqual(self.db.jobs[self.JOB_A]["worker_id"], "kitchen-worker-1")
		self.assertEqual(
			self.db.heartbeats["kitchen-worker-1"],
			{
				"doctype": "Local Printer Worker Heartbeat",
				"worker_id": "kitchen-worker-1",
				"user": "worker@example.com",
				"last_seen": NOW,
			},
		)

	def test_worker_acknowledges_claimed_job(self):
		self.bind_worker()
		self.db.add_job(
			self.JOB_A,
			status="Printing",
			attempt_count=1,
			worker_id="kitchen-worker-1",
		)

		result = print_jobs.acknowledge(
			self.JOB_A,
			"kitchen-worker-1",
			success=1,
		)

		self.assertEqual(result, {"status": "Success"})
		self.assertEqual(self.db.jobs[self.JOB_A]["status"], "Success")
		self.assertEqual(self.db.jobs[self.JOB_A]["printed_at"], NOW)

	def test_cashier_cannot_claim_jobs(self):
		frappe._test_roles = ["Cashier"]

		with self.assertRaises(frappe.PermissionError):
			print_jobs.claim_jobs("kitchen-worker-1")

	def test_guest_cannot_use_any_print_job_api(self):
		frappe.session.user = "Guest"
		calls = (
			("claim", lambda: print_jobs.claim_jobs("kitchen-worker-1")),
			(
				"acknowledge",
				lambda: print_jobs.acknowledge(
					self.JOB_A, "kitchen-worker-1", success=1
				),
			),
			("retry", lambda: print_jobs.retry_failed(self.JOB_A)),
			("status", lambda: print_jobs.get_status("Main POS")),
		)

		for endpoint, call in calls:
			with self.subTest(endpoint=endpoint), self.assertRaises(
				frappe.AuthenticationError
			):
				call()

	def test_another_account_cannot_claim_with_an_owned_worker_id(self):
		self.bind_worker(user="first-worker@example.com")
		original = dict(self.db.heartbeats["kitchen-worker-1"])
		frappe.session.user = "second-worker@example.com"

		with self.assertRaises(frappe.PermissionError):
			print_jobs.claim_jobs("kitchen-worker-1")

		self.assertEqual(self.db.heartbeats["kitchen-worker-1"], original)

	def test_another_account_cannot_acknowledge_with_an_owned_worker_id(self):
		self.bind_worker(user="first-worker@example.com")
		self.db.add_job(
			self.JOB_A,
			status="Printing",
			attempt_count=1,
			worker_id="kitchen-worker-1",
		)
		frappe.session.user = "second-worker@example.com"

		with self.assertRaises(frappe.PermissionError):
			print_jobs.acknowledge(
				self.JOB_A,
				"kitchen-worker-1",
				success=1,
			)

		self.assertEqual(self.db.jobs[self.JOB_A]["status"], "Printing")

	def test_existing_worker_heartbeat_is_updated_for_its_owner(self):
		self.bind_worker()

		print_jobs.claim_jobs("kitchen-worker-1")

		self.assertEqual(
			self.db.heartbeats["kitchen-worker-1"]["last_seen"], NOW
		)
		self.assertEqual(
			self.db.heartbeats["kitchen-worker-1"]["user"],
			"worker@example.com",
		)

	def test_duplicate_worker_registration_race_recovers_for_same_owner(self):
		self.bind_worker()
		self.db.heartbeat_snapshot_stale = True

		print_jobs.claim_jobs("kitchen-worker-1")

		self.assertEqual(
			self.db.heartbeats["kitchen-worker-1"]["last_seen"], NOW
		)
		self.assertEqual(
			self.db.heartbeat_current_reads,
			[{"worker_id": "kitchen-worker-1"}],
		)

	def test_duplicate_worker_registration_race_cannot_replace_owner(self):
		self.bind_worker(user="first-worker@example.com")
		original = dict(self.db.heartbeats["kitchen-worker-1"])
		self.db.heartbeat_snapshot_stale = True
		frappe.session.user = "second-worker@example.com"

		with self.assertRaises(frappe.PermissionError):
			print_jobs.claim_jobs("kitchen-worker-1")

		self.assertEqual(self.db.heartbeats["kitchen-worker-1"], original)
		self.assertEqual(
			self.db.heartbeat_current_reads,
			[{"worker_id": "kitchen-worker-1"}],
		)

	def test_manager_and_system_manager_can_retry_failed_job(self):
		for role in ("Restaurant Manager", "System Manager"):
			with self.subTest(role=role):
				self.db.add_job(self.JOB_A, status="Failed", attempt_count=3)
				frappe._test_roles = [role]
				frappe.session.user = f"{role.lower().replace(' ', '-')}@example.com"

				result = print_jobs.retry_failed(self.JOB_A)

				self.assertEqual(result, {"status": "Pending"})
				job = self.db.jobs[self.JOB_A]
				self.assertEqual(job["attempt_count"], 0)
				self.assertIsNone(job["worker_id"])
				self.assertIsNone(job["claimed_at"])
				self.assertIsNone(job["error_message"])
				self.assertEqual(job["requested_by"], frappe.session.user)

	def test_waiter_cannot_retry_failed_job(self):
		self.db.add_job(self.JOB_A, status="Failed", attempt_count=3)
		frappe._test_roles = ["Waiter"]

		with self.assertRaises(frappe.PermissionError):
			print_jobs.retry_failed(self.JOB_A)

	def test_retry_rejects_a_job_that_is_not_failed(self):
		self.db.add_job(self.JOB_A, status="Pending")
		frappe._test_roles = ["Restaurant Manager"]

		with self.assertRaises(frappe.ValidationError):
			print_jobs.retry_failed(self.JOB_A)

	def test_status_uses_heartbeat_and_counts_only_requested_pos_profile(self):
		self.db.add_job(self.JOB_A, pos_profile="Main POS", status="Pending")
		self.db.add_job(self.JOB_B, pos_profile="Main POS", status="Failed", attempt_count=3)
		self.db.add_job(self.JOB_C, pos_profile="Other POS", status="Failed", attempt_count=3)
		self.db.heartbeats["kitchen-worker-1"] = {
			"worker_id": "kitchen-worker-1",
			"user": "worker@example.com",
			"last_seen": NOW - timedelta(seconds=30),
		}
		frappe._test_roles = ["Restaurant Manager"]

		result = print_jobs.get_status("Main POS")

		self.assertEqual(
			result,
			{
				"online": True,
				"last_seen": "2026-09-03 11:59:30",
				"pending": 1,
				"failed": 1,
			},
		)

	def test_status_reports_offline_without_a_recent_heartbeat(self):
		self.db.heartbeats["kitchen-worker-1"] = {
			"worker_id": "kitchen-worker-1",
			"last_seen": NOW - timedelta(minutes=10),
		}
		frappe._test_roles = ["Cashier"]

		result = print_jobs.get_status("Main POS")

		self.assertFalse(result["online"])
		self.assertEqual(result["last_seen"], "2026-09-03 11:50:00")

	def test_status_requires_access_to_requested_pos_profile(self):
		frappe._test_roles = ["Cashier"]
		frappe._test_has_permission = False

		with self.assertRaises(frappe.PermissionError):
			print_jobs.get_status("Main POS")

	def test_status_rejects_waiter_and_unknown_pos_profile(self):
		frappe._test_roles = ["Waiter"]
		with self.assertRaises(frappe.PermissionError):
			print_jobs.get_status("Main POS")

		frappe._test_roles = ["Restaurant Manager"]
		with self.assertRaises(frappe.ValidationError):
			print_jobs.get_status("Missing POS")

	def test_worker_and_job_identifiers_are_validated(self):
		for worker_id in (None, "", " worker ", "worker?one", "x" * 141):
			with self.subTest(worker_id=worker_id), self.assertRaises(
				frappe.ValidationError
			):
				print_jobs.claim_jobs(worker_id)

		self.db.add_job(
			self.JOB_A,
			status="Printing",
			attempt_count=1,
			worker_id="kitchen-worker-1",
		)
		for job_id in (None, "", "not-a-uuid"):
			with self.subTest(job_id=job_id), self.assertRaises(
				frappe.ValidationError
			):
				print_jobs.acknowledge(job_id, "kitchen-worker-1", success=1)

	def test_limit_is_positive_integer_and_clamped(self):
		for limit in (0, -1, "invalid", True):
			with self.subTest(limit=limit), self.assertRaises(frappe.ValidationError):
				print_jobs.claim_jobs("kitchen-worker-1", limit=limit)

		print_jobs.claim_jobs("kitchen-worker-1", limit=500)
		self.assertEqual(self.db.last_claim_limit, 50)

		print_jobs.claim_jobs("kitchen-worker-1", limit="9" * 10000)
		self.assertEqual(self.db.last_claim_limit, 50)

	def test_acknowledgement_validates_success_and_error(self):
		self.bind_worker()
		self.db.add_job(
			self.JOB_A,
			status="Printing",
			attempt_count=1,
			worker_id="kitchen-worker-1",
		)

		for success in (-1, 2, "yes", None, 0.0, 1.0):
			with self.subTest(success=success), self.assertRaises(
				frappe.ValidationError
			):
				print_jobs.acknowledge(
					self.JOB_A,
					"kitchen-worker-1",
					success=success,
				)

		with self.assertRaises(frappe.ValidationError):
			print_jobs.acknowledge(
				self.JOB_A,
				"kitchen-worker-1",
				success=0,
				error="x" * 2001,
			)

	def test_different_worker_cannot_acknowledge(self):
		self.bind_worker(worker_id="kitchen-worker-2")
		self.db.add_job(
			self.JOB_A,
			status="Printing",
			attempt_count=1,
			worker_id="kitchen-worker-1",
		)

		with self.assertRaises(frappe.ValidationError):
			print_jobs.acknowledge(
				self.JOB_A,
				"kitchen-worker-2",
				success=1,
			)

	def test_uppercase_uuid_is_normalized_before_acknowledgement(self):
		self.bind_worker()
		self.db.add_job(
			self.JOB_WITH_HEX,
			status="Printing",
			attempt_count=1,
			worker_id="kitchen-worker-1",
		)

		result = print_jobs.acknowledge(
			self.JOB_WITH_HEX.upper(),
			"kitchen-worker-1",
			success="1",
		)

		self.assertEqual(result, {"status": "Success"})

	def test_automatic_claim_never_exceeds_three_attempts(self):
		self.db.add_job(self.JOB_A, attempt_count=3)
		self.db.job_snapshot_attempt_counts[self.JOB_A] = 2

		result = print_jobs.claim_jobs("kitchen-worker-1")

		self.assertEqual(result, {"jobs": []})
		self.assertEqual(self.db.jobs[self.JOB_A]["attempt_count"], 3)

	def test_claim_increments_the_current_locked_attempt_count(self):
		self.db.add_job(self.JOB_A, attempt_count=2)
		self.db.job_snapshot_attempt_counts[self.JOB_A] = 1

		result = print_jobs.claim_jobs("kitchen-worker-1")

		self.assertEqual(result["jobs"][0]["attempt_count"], 3)
		self.assertEqual(self.db.jobs[self.JOB_A]["attempt_count"], 3)


if __name__ == "__main__":
	unittest.main()
