# Copyright (c) 2024, mohammed hassan and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class PrinterItemGroup(Document):
	def validate(self):
		self._validate_configuration_type()
		self._validate_default_kitchen_is_unique()

	def _validate_configuration_type(self):
		if cint(self.is_cashier) and cint(self.is_default_kitchen):
			frappe.throw(
				_("A cashier printer cannot also be the default kitchen printer."),
				frappe.ValidationError,
			)

		if cint(self.is_default_kitchen) and self.target_doctype != "Sales Order":
			frappe.throw(
				_("A default kitchen printer must target Sales Order."),
				frappe.ValidationError,
			)

		if cint(self.is_cashier):
			if self.target_doctype != "Sales Invoice":
				frappe.throw(
					_("A cashier printer must target Sales Invoice."),
					frappe.ValidationError,
				)
			if self.trigger_method != "manual":
				frappe.throw(
					_("A cashier printer must use the manual trigger method."),
					frappe.ValidationError,
				)

	def _validate_default_kitchen_is_unique(self):
		if not (cint(self.enabled) and cint(self.is_default_kitchen)):
			return

		filters = {
			"pos_profile": self.pos_profile,
			"enabled": 1,
			"is_default_kitchen": 1,
		}
		if self.name:
			filters["name"] = ["!=", self.name]

		if frappe.db.exists("Printer Item Group", filters):
			frappe.throw(
				_("Only one enabled default kitchen printer is allowed for POS Profile {0}.").format(
					self.pos_profile
				),
				frappe.ValidationError,
			)
