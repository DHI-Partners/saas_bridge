import frappe


def _as_list(value):
	if not value:
		return []
	if isinstance(value, str):
		return frappe.parse_json(value)
	return value


@frappe.whitelist()
def get_roles(user):
	frappe.only_for("System Manager")
	doc = frappe.get_doc("User", user)
	return {
		"user": user,
		"role_profile": doc.role_profile_name,
		"roles": [r.role for r in doc.roles],
	}


@frappe.whitelist()
def set_role_profile(user, role_profile):
	"""Replace the user's role profile. Frappe applies the profile's roles on save."""
	frappe.only_for("System Manager")
	doc = frappe.get_doc("User", user)
	doc.role_profile_name = role_profile
	doc.save(ignore_permissions=True)
	return get_roles(user)


@frappe.whitelist()
def add_roles(user, roles):
	"""Add one or more roles on top of whatever the user already has."""
	frappe.only_for("System Manager")
	roles = _as_list(roles)
	doc = frappe.get_doc("User", user)
	existing = {r.role for r in doc.roles}
	for role in roles:
		if role not in existing:
			doc.append("roles", {"role": role})
	doc.save(ignore_permissions=True)
	return get_roles(user)


@frappe.whitelist()
def remove_roles(user, roles):
	"""Remove one or more roles, leaving the rest untouched."""
	frappe.only_for("System Manager")
	roles = set(_as_list(roles))
	doc = frappe.get_doc("User", user)
	doc.roles = [r for r in doc.roles if r.role not in roles]
	doc.save(ignore_permissions=True)
	return get_roles(user)


@frappe.whitelist()
def create_user(email, first_name, role_profile=None, roles=None, send_welcome_email=0):
	"""Provision a user for a client (e.g. right after bench new-site), with a plan's role profile."""
	frappe.only_for("System Manager")
	if frappe.db.exists("User", email):
		frappe.throw(f"User {email} already exists")

	doc = frappe.new_doc("User")
	doc.email = email
	doc.first_name = first_name
	doc.send_welcome_email = frappe.utils.cint(send_welcome_email)
	if role_profile:
		doc.role_profile_name = role_profile
	doc.insert(ignore_permissions=True)

	for role in _as_list(roles):
		doc.append("roles", {"role": role})
	if roles:
		doc.save(ignore_permissions=True)

	return get_roles(email)
