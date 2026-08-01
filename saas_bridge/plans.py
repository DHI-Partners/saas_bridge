"""Single source of truth for which Role Profiles the control plane manages.

Both plans and add-ons are modelled as Role Profiles because Frappe v16 re-derives
`User.roles` from the `role_profiles` child table on every save — a role appended
directly to `User.roles` is dropped again as soon as any profile is attached.
Since `role_profiles` is a multi-select, a user can hold one plan profile plus any
number of add-on profiles, and Frappe unions their roles.

The role lists below are the seed used by `after_install`; existing Role Profiles are
never modified, so tune them here before install or in the Frappe UI afterwards. Note that
ERPNext auto-manages the "Employee" role (`validate_employee_role` strips it from users
without a linked Employee record), so listing it in a profile has no effect.
"""

PLAN_ROLE_PROFILES = {
	"Starter": [
		"Accounts User",
		"Stock User",
	],
	"Pro": [
		"Accounts User",
		"Stock User",
		"Sales User",
		"Purchase User",
		"Item Manager",
		"Projects User",
	],
	"Enterprise": [
		"Accounts User",
		"Accounts Manager",
		"Stock User",
		"Stock Manager",
		"Sales User",
		"Sales Manager",
		"Purchase User",
		"Purchase Manager",
		"Item Manager",
		"Projects User",
	],
}

ADDON_ROLE_PROFILES = {
	"WhatsApp Integration": ["WhatsApp Integration"],
	"Telegram Integration": ["Telegram Integration"],
}

#: Roles owned by this app (created on install); plan roles come from frappe/erpnext.
ADDON_ROLES = sorted({role for roles in ADDON_ROLE_PROFILES.values() for role in roles})
