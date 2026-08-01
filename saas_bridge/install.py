import frappe

from saas_bridge.plans import ADDON_ROLE_PROFILES, ADDON_ROLES, PLAN_ROLE_PROFILES


def after_install():
	_create_roles()
	_create_role_profiles()


def _create_roles():
	for role in ADDON_ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
				ignore_permissions=True
			)


def _create_role_profiles():
	"""Seed the plan/add-on profiles. Existing profiles are left untouched so a site's
	own tuning of the role lists survives a reinstall."""
	for profile, roles in {**PLAN_ROLE_PROFILES, **ADDON_ROLE_PROFILES}.items():
		if frappe.db.exists("Role Profile", profile):
			continue

		doc = frappe.new_doc("Role Profile")
		doc.role_profile = profile
		for role in roles:
			if frappe.db.exists("Role", role):
				doc.append("roles", {"role": role})
			else:
				frappe.log_error(
					title="SaaS Bridge install",
					message=f"Role {role!r} missing, skipped for Role Profile {profile!r}",
				)
		doc.insert(ignore_permissions=True)
