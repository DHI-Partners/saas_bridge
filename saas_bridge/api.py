import frappe
from frappe.utils import cint, now, validate_email_address
from frappe.utils.background_jobs import is_job_enqueued

from saas_bridge import provision


@frappe.whitelist(methods=["POST"])
def create_site(
	site=None,
	apps=None,
	admin_password=None,
	email=None,
	password=None,
	first_name=None,
	last_name=None,
):
	"""Create a new site with the requested apps and a login for it.

	Everything is validated here, synchronously, so a bad request fails fast with a real
	error. The install itself is enqueued: `bench new-site` with a few apps runs for
	minutes, well past the gunicorn request timeout. Poll `get_site_status` for the
	outcome.

	Generated passwords are returned in this response only — they are never stored in the
	run state, so this is the caller's one chance to keep them.
	"""
	frappe.only_for("System Manager")

	site = provision.ensure_site_available(provision.normalize_site_name(site))
	apps = provision.validate_apps(apps)

	# fail here rather than in the worker, where a config gap only surfaces on polling
	provision.bench_command()
	provision.db_root_password()

	generated = {}
	if not admin_password:
		admin_password = frappe.generate_hash(length=16)
		generated["admin_password"] = admin_password
	provision.validate_password(admin_password, "`admin_password`")

	if email:
		validate_email_address(email, throw=True)
		if not password:
			password = frappe.generate_hash(length=16)
			generated["password"] = password
		provision.validate_password(password, "`password`")
		first_name = first_name or email.split("@")[0]
	elif password:
		frappe.throw("`password` was given without an `email` to create the login for")

	# the job id is checked too, not just the run state: the state carries a TTL and can be
	# evicted from redis, and reporting "queued" over a live run would hide its progress
	job_id = f"saas-bridge-provision-{site}"
	state = provision.get_state(site)
	if (state and state.get("status") in ("queued", "running")) or is_job_enqueued(job_id):
		frappe.throw(f"Provisioning of {site} is already in progress")

	provision.set_state(
		site,
		status="queued",
		apps=apps,
		login=email,
		queued_at=now(),
		error=None,
		cleanup=None,
		apps_installed=None,
		login_created=None,
		finished_at=None,
	)

	# deduplicate closes the race between two requests that both passed the check above:
	# the loser gets no job back rather than a second `bench new-site` for the same site
	job = frappe.enqueue(
		"saas_bridge.provision.provision_site",
		queue="long",
		# both bench steps can each take the full step timeout, plus slack for the job itself
		timeout=2 * provision.step_timeout() + 60,
		job_id=job_id,
		deduplicate=True,
		site=site,
		apps=apps,
		admin_password=admin_password,
		email=email,
		password=password,
		first_name=first_name,
		last_name=last_name,
	)

	if job is None:
		frappe.throw(f"Provisioning of {site} is already in progress")

	return {
		"site": site,
		"apps": apps,
		"login": email,
		"status": "queued",
		"job_id": job.id,
		**generated,
	}


@frappe.whitelist()
def get_site_status(site=None):
	"""Progress of a `create_site` call: queued, running, success or failed."""
	frappe.only_for("System Manager")

	site = provision.normalize_site_name(site)
	state = provision.get_state(site)
	if not state:
		frappe.throw(f"No provisioning run recorded for {site}")
	return state


@frappe.whitelist(methods=["POST"])
def set_site_language(site=None, language="ru", enabled=1):
	"""Enable (or disable) a language on a site of this bench — Russian by default.

	Runs synchronously: unlike `bench new-site`, this is one row written on a site that
	already exists, which fits inside a request.

	The write goes through `frappe.client.set_value` rather than `frappe.db.set_value` so
	the Language document is saved properly — `on_update` is what drops the cached language
	list, and without it the target site would keep serving the old set of languages until
	its cache was cleared by something else.
	"""
	frappe.only_for("System Manager")

	site = provision.ensure_site_exists(provision.normalize_site_name(site))
	language = provision.validate_language(language)
	enabled = 1 if cint(enabled) else 0

	# fail here rather than inside the subprocess, where a config gap reads as a bench error
	provision.bench_command()

	try:
		provision.execute_on_site(
			site,
			"frappe.client.set_value",
			# a dict rather than fieldname/value: `client.set_value` treats a falsy `value`
			# as "no value given", so passing `enabled=0` positionally would not disable it
			{"doctype": "Language", "name": language, "fieldname": {"enabled": enabled}},
			label=f"`bench --site {site} execute` for language {language}",
		)
	except provision.ProvisionError as exc:
		frappe.throw(f"Could not set language {language} on {site}: {exc}")

	return {"site": site, "language": language, "enabled": enabled}


@frappe.whitelist()
def get_available_apps():
	"""Apps that `create_site` will accept on this bench."""
	frappe.only_for("System Manager")

	available = frappe.get_all_apps(with_internal_apps=False)
	allowed = frappe.conf.get("saas_bridge_allowed_apps")
	return [app for app in available if app != "frappe" and (not allowed or app in allowed)]
