import frappe

ADDON_ROLES = ["WhatsApp Integration", "Telegram Integration"]


def after_install():
	for role in ADDON_ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
				ignore_permissions=True
			)
