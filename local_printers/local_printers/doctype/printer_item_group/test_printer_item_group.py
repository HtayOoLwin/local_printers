# Copyright (c) 2024, mohammed hassan and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase


class TestPrinterItemGroup(FrappeTestCase):
	def _new_configuration(self, **overrides):
		"""Build a configuration without requiring ERPNext fixture records."""
		defaults = {
			"doctype": "Printer Item Group",
			"pos_profile": f"Test POS Profile {frappe.generate_hash(length=8)}",
			"target_doctype": "Sales Order",
			"trigger_method": "on_submit",
			"enabled": 1,
			"is_cashier": 0,
			"is_default_kitchen": 0,
			"printer": f"Test Printer {frappe.generate_hash(length=8)}",
			"printer_ip": "127.0.0.1",
			"printer_item_group": [{"item_group": "All Item Groups"}],
		}
		defaults.update(overrides)
		return frappe.get_doc(defaults)

	def test_duplicate_enabled_default_kitchens_are_rejected(self):
		pos_profile = f"Test POS Profile {frappe.generate_hash(length=8)}"
		self._new_configuration(
			pos_profile=pos_profile, is_default_kitchen=1
		).insert(ignore_permissions=True, ignore_links=True)

		with self.assertRaises(frappe.ValidationError):
			self._new_configuration(
				pos_profile=pos_profile, is_default_kitchen=1
			).insert(ignore_permissions=True, ignore_links=True)

	def test_disabled_default_kitchens_can_coexist(self):
		pos_profile = f"Test POS Profile {frappe.generate_hash(length=8)}"
		disabled_default = self._new_configuration(
			pos_profile=pos_profile, enabled=0, is_default_kitchen=1
		).insert(ignore_permissions=True, ignore_links=True)
		self._new_configuration(
			pos_profile=pos_profile, is_default_kitchen=1
		).insert(ignore_permissions=True, ignore_links=True)

		self.assertEqual(disabled_default.enabled, 0)

	def test_cashier_cannot_be_default_kitchen(self):
		configuration = self._new_configuration(
			target_doctype="Sales Invoice",
			trigger_method="manual",
			is_cashier=1,
			is_default_kitchen=1,
		)

		with self.assertRaises(frappe.ValidationError):
			configuration.run_method("validate")

	def test_invalid_target_and_trigger_combinations_are_rejected(self):
		invalid_configurations = [
			{"target_doctype": "Sales Invoice", "is_default_kitchen": 1},
			{
				"target_doctype": "Sales Order",
				"trigger_method": "manual",
				"is_cashier": 1,
			},
			{
				"target_doctype": "Sales Invoice",
				"trigger_method": "on_submit",
				"is_cashier": 1,
			},
		]

		for overrides in invalid_configurations:
			with self.subTest(overrides=overrides), self.assertRaises(frappe.ValidationError):
				self._new_configuration(**overrides).run_method("validate")

	def test_default_kitchen_requires_on_submit_trigger(self):
		for trigger_method in ("manual", "on_cancel"):
			with self.subTest(trigger_method=trigger_method), self.assertRaisesRegex(
				frappe.ValidationError,
				"A default kitchen printer must use the on_submit trigger method.",
			):
				self._new_configuration(
					is_default_kitchen=1, trigger_method=trigger_method
				).run_method("validate")

	def test_valid_default_kitchen_and_cashier_configurations(self):
		self._new_configuration(is_default_kitchen=1).run_method("validate")
		self._new_configuration(
			target_doctype="Sales Invoice", trigger_method="manual", is_cashier=1
		).run_method("validate")
