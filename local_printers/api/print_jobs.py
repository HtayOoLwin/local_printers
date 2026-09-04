import re
import uuid
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

from local_printers.printing.jobs import (
	acknowledge_job,
	claim_next_jobs,
	retry_failed_job,
)


WORKER_ROLE = "Local Printer Worker"
RETRY_ROLES = frozenset(("Restaurant Manager", "System Manager"))
STATUS_ROLES = frozenset(("Cashier", "Restaurant Manager", "System Manager"))

MAX_CLAIM_LIMIT = 50
MAX_ERROR_LENGTH = 2000
ONLINE_WINDOW = timedelta(minutes=2)
WORKER_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,139}\Z")


def _require_roles(allowed_roles: frozenset[str]) -> None:
	user = getattr(frappe.session, "user", None)
	if not user or user == "Guest":
		frappe.throw(_("Authentication is required."), frappe.AuthenticationError)

	if not allowed_roles.intersection(frappe.get_roles(user)):
		frappe.throw(_("You are not permitted to perform this action."), frappe.PermissionError)


def _validated_worker_id(worker_id: str) -> str:
	if not isinstance(worker_id, str) or not WORKER_ID_PATTERN.fullmatch(worker_id):
		frappe.throw(_("Invalid worker ID."), frappe.ValidationError)
	return worker_id


def _validated_job_id(job_id: str) -> str:
	if not isinstance(job_id, str):
		frappe.throw(_("Invalid print job ID."), frappe.ValidationError)
	try:
		parsed = uuid.UUID(job_id)
	except (ValueError, AttributeError):
		frappe.throw(_("Invalid print job ID."), frappe.ValidationError)
	if str(parsed) != job_id.lower():
		frappe.throw(_("Invalid print job ID."), frappe.ValidationError)
	return str(parsed)


def _validated_limit(limit: int) -> int:
	if isinstance(limit, bool):
		frappe.throw(_("Claim limit must be a positive integer."), frappe.ValidationError)
	if isinstance(limit, str):
		value = limit.strip()
		if not value.isdigit():
			frappe.throw(_("Claim limit must be a positive integer."), frappe.ValidationError)
		limit = int(value)
	elif not isinstance(limit, int):
		frappe.throw(_("Claim limit must be a positive integer."), frappe.ValidationError)

	if limit <= 0:
		frappe.throw(_("Claim limit must be a positive integer."), frappe.ValidationError)
	return min(limit, MAX_CLAIM_LIMIT)


def _validated_success(success: int) -> bool:
	if isinstance(success, bool):
		return success
	if type(success) is int and success in (0, 1):
		return bool(success)
	if isinstance(success, str) and success in ("0", "1"):
		return success == "1"
	frappe.throw(_("Success must be either 0 or 1."), frappe.ValidationError)


def _validated_error(error: str | None) -> str | None:
	if error is None:
		return None
	if not isinstance(error, str) or len(error) > MAX_ERROR_LENGTH:
		frappe.throw(_("Invalid printer error."), frappe.ValidationError)
	return error


def _validated_pos_profile(pos_profile: str) -> str:
	if (
		not isinstance(pos_profile, str)
		or not pos_profile
		or pos_profile != pos_profile.strip()
		or len(pos_profile) > 140
		or any(character in pos_profile for character in "\r\n\0")
	):
		frappe.throw(_("Invalid POS Profile."), frappe.ValidationError)
	if not frappe.db.exists("POS Profile", pos_profile):
		frappe.throw(_("Unknown POS Profile."), frappe.ValidationError)
	return pos_profile


def _worker_owner(worker_id: str) -> str | None:
	return frappe.db.get_value(
		"Local Printer Worker Heartbeat",
		worker_id,
		"user",
	)


def _locked_worker_owner(worker_id: str) -> str | None:
	rows = frappe.db.sql(
		"""
		SELECT `user`
		FROM `tabLocal Printer Worker Heartbeat`
		WHERE name = %(worker_id)s
		FOR UPDATE
		""",
		{"worker_id": worker_id},
		as_dict=True,
	)
	return rows[0].user if rows else None


def _require_worker_owner(worker_id: str) -> None:
	if _worker_owner(worker_id) != frappe.session.user:
		frappe.throw(
			_("This worker ID belongs to a different authenticated user."),
			frappe.PermissionError,
		)


def _update_worker_heartbeat(worker_id: str) -> None:
	frappe.db.set_value(
		"Local Printer Worker Heartbeat",
		worker_id,
		"last_seen",
		now_datetime(),
		update_modified=False,
	)


def _register_or_touch_worker(worker_id: str) -> None:
	if _worker_owner(worker_id):
		_require_worker_owner(worker_id)
		_update_worker_heartbeat(worker_id)
		return

	try:
		frappe.get_doc(
			{
				"doctype": "Local Printer Worker Heartbeat",
				"worker_id": worker_id,
				"user": frappe.session.user,
				"last_seen": now_datetime(),
			}
		).insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		# Locking reads see the concurrently committed binding even when this
		# transaction's ordinary reads still use an older repeatable-read snapshot.
		if _locked_worker_owner(worker_id) != frappe.session.user:
			frappe.throw(
				_("This worker ID belongs to a different authenticated user."),
				frappe.PermissionError,
			)
		_update_worker_heartbeat(worker_id)


def _latest_heartbeat():
	rows = frappe.get_all(
		"Local Printer Worker Heartbeat",
		fields=("last_seen",),
		filters={"last_seen": ["is", "set"]},
		order_by="last_seen desc",
		limit=1,
	)
	return get_datetime(rows[0].last_seen) if rows else None


@frappe.whitelist(methods=["POST"])
def claim_jobs(worker_id: str, limit: int = 10) -> dict:
	"""Claim pending jobs for an authenticated Windows print worker."""
	_require_roles(frozenset((WORKER_ROLE,)))
	worker_id = _validated_worker_id(worker_id)
	limit = _validated_limit(limit)
	_register_or_touch_worker(worker_id)
	return {"jobs": claim_next_jobs(worker_id, limit)}


@frappe.whitelist(methods=["POST"])
def acknowledge(
	job_id: str,
	worker_id: str,
	success: int,
	error: str | None = None,
) -> dict:
	"""Acknowledge a claim; success accepts bool/int 0/1 or strings ``0``/``1``."""
	_require_roles(frozenset((WORKER_ROLE,)))
	job_id = _validated_job_id(job_id)
	worker_id = _validated_worker_id(worker_id)
	_require_worker_owner(worker_id)
	success_value = _validated_success(success)
	error = _validated_error(error)
	result = acknowledge_job(job_id, worker_id, success_value, error)
	return {"status": result["status"]}


@frappe.whitelist(methods=["POST"])
def retry_failed(job_id: str) -> dict:
	"""Requeue a failed job for a fresh, bounded automatic attempt cycle."""
	_require_roles(RETRY_ROLES)
	job_id = _validated_job_id(job_id)
	result = retry_failed_job(job_id)
	return {"status": result["status"]}


@frappe.whitelist()
def get_status(pos_profile: str) -> dict:
	"""Return worker presence and job counts within one permitted POS Profile."""
	_require_roles(STATUS_ROLES)
	pos_profile = _validated_pos_profile(pos_profile)
	if "System Manager" not in frappe.get_roles(frappe.session.user) and not frappe.has_permission(
		"POS Profile",
		ptype="read",
		doc=pos_profile,
	):
		frappe.throw(_("You cannot view this POS Profile."), frappe.PermissionError)

	last_seen = _latest_heartbeat()
	online = bool(last_seen and now_datetime() - last_seen <= ONLINE_WINDOW)
	return {
		"online": online,
		"last_seen": str(last_seen) if last_seen else None,
		"pending": frappe.db.count(
			"Local Print Job",
			{"pos_profile": pos_profile, "status": "Pending"},
		),
		"failed": frappe.db.count(
			"Local Print Job",
			{"pos_profile": pos_profile, "status": "Failed"},
		),
	}
