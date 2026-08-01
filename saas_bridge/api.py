import frappe

from saas_bridge.plans import ADDON_ROLE_PROFILES, PLAN_ROLE_PROFILES


def _as_list(value):
	if not value:
		return []
	if isinstance(value, str):
		return frappe.parse_json(value)
	return value


def _profiles_of(doc):
	return [row.role_profile for row in doc.role_profiles]


def _roles_of(profiles):
	roles = set()
	for profile in profiles:
		if frappe.db.exists("Role Profile", profile):
			roles.update(row.role for row in frappe.get_cached_doc("Role Profile", profile).roles)
	return roles


def _apply_profiles(doc, profiles):
	"""Replace the user's role profiles and save.

	Two Frappe v16 quirks are handled here:

	* `roles` is only rebuilt from the profiles while at least one profile is attached
	  (`User.populate_role_profile_roles` returns early on an empty table), so roles
	  granted by profiles that are going away are stripped explicitly. That is what
	  makes cancelling the last subscription actually revoke access.
	* the deprecated `role_profile_name` field is re-appended to the table on validate
	  (`User.move_role_profile_name_to_role_profiles`), which would silently resurrect
	  the profile we just removed. Clearing it first keeps the removal.
	"""
	dropped = _roles_of(set(_profiles_of(doc)) - set(profiles)) - _roles_of(profiles)
	doc.role_profile_name = None
	doc.set("role_profiles", [{"role_profile": profile} for profile in profiles])
	if dropped:
		doc.set("roles", [row for row in doc.roles if row.role not in dropped])
	doc.save(ignore_permissions=True)


def _validate(profile, known, label):
	if profile not in known:
		frappe.throw(f"Unknown {label}: {profile}")


@frappe.whitelist()
def get_roles(user):
	frappe.only_for("System Manager")
	doc = frappe.get_doc("User", user)
	profiles = _profiles_of(doc)
	return {
		"user": user,
		"plan": next((p for p in profiles if p in PLAN_ROLE_PROFILES), None),
		"addons": [p for p in profiles if p in ADDON_ROLE_PROFILES],
		"role_profiles": profiles,
		"roles": sorted(row.role for row in doc.roles),
	}


@frappe.whitelist()
def set_plan(user, plan=None):
	"""Switch the user to a plan profile, keeping any purchased add-on profiles.

	An empty `plan` cancels the subscription: the plan's roles are revoked and only
	the add-on profiles remain.
	"""
	frappe.only_for("System Manager")
	if plan:
		_validate(plan, PLAN_ROLE_PROFILES, "plan role profile")

	doc = frappe.get_doc("User", user)
	profiles = [p for p in _profiles_of(doc) if p not in PLAN_ROLE_PROFILES]
	if plan:
		profiles.insert(0, plan)

	_apply_profiles(doc, profiles)
	return get_roles(user)


@frappe.whitelist()
def add_addon(user, addon):
	frappe.only_for("System Manager")
	_validate(addon, ADDON_ROLE_PROFILES, "addon role profile")

	doc = frappe.get_doc("User", user)
	profiles = _profiles_of(doc)
	if addon in profiles:
		return get_roles(user)

	_apply_profiles(doc, [*profiles, addon])
	return get_roles(user)


@frappe.whitelist()
def remove_addon(user, addon):
	frappe.only_for("System Manager")
	_validate(addon, ADDON_ROLE_PROFILES, "addon role profile")

	doc = frappe.get_doc("User", user)
	profiles = _profiles_of(doc)
	if addon not in profiles:
		return get_roles(user)

	_apply_profiles(doc, [p for p in profiles if p != addon])
	return get_roles(user)


@frappe.whitelist()
def create_user(email, first_name, plan=None, addons=None, send_welcome_email=0):
	"""Provision a user for a client (e.g. right after bench new-site) on a given plan."""
	frappe.only_for("System Manager")
	if frappe.db.exists("User", email):
		frappe.throw(f"User {email} already exists")

	addons = _as_list(addons)
	if plan:
		_validate(plan, PLAN_ROLE_PROFILES, "plan role profile")
	for addon in addons:
		_validate(addon, ADDON_ROLE_PROFILES, "addon role profile")

	doc = frappe.new_doc("User")
	doc.email = email
	doc.first_name = first_name
	doc.send_welcome_email = frappe.utils.cint(send_welcome_email)
	for profile in ([plan] if plan else []) + addons:
		doc.append("role_profiles", {"role_profile": profile})
	doc.insert(ignore_permissions=True)

	return get_roles(email)
