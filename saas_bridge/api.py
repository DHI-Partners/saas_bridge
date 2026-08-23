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
	max_users=None,
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
	limits = provision.validate_limits(max_users)

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
		limits=limits,
	)

	if job is None:
		frappe.throw(f"Provisioning of {site} is already in progress")

	return {
		"site": site,
		"apps": apps,
		"login": email,
		"status": "queued",
		"job_id": job.id,
		**limits,
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
def set_site_apps(site=None, install=None, uninstall=None, backup=1):
	"""Add and remove apps on a site that already exists.

	One endpoint for both directions because they are one intent: a plan change is usually a
	swap, and doing it in a single run means the two halves cannot interleave with another
	request's. Removals run first — see `provision.change_site_apps`.

	Validated synchronously against the site's own list of installed apps, then enqueued:
	installing an app migrates the whole site, which runs for minutes. Poll
	`get_site_apps_status` for the outcome.

	Uninstalling drops the app's doctypes and their data from the site, and takes a backup
	first unless `backup` is false.

	An app that declares `required_apps` brings them with it: bench installs those first, so
	a site can end up with more apps than were asked for. The finished run reports what was
	requested — read the site's app list back for what it actually has.
	"""
	frappe.only_for("System Manager")

	site = provision.ensure_site_exists(provision.normalize_site_name(site))

	# the site serving this request is the one running this code: uninstalling an app from
	# it would pull the desk apart under the caller, and the worker doing it holds this same
	# site's connection. Bench changes to this site belong on the command line
	if site == frappe.local.site and uninstall:
		frappe.throw(f"{site} is the site serving this request — uninstall apps from it with bench")

	install, uninstall = provision.validate_app_changes(site, install, uninstall)
	backup = 1 if cint(backup) else 0

	# fail here rather than in the worker, where a config gap only surfaces on polling
	provision.bench_command()

	# a site being built has no settled app list to change, and the two jobs would be
	# running `bench` against the same site at the same time
	provisioning = provision.get_state(site)
	if provisioning and provisioning.get("status") in ("queued", "running"):
		frappe.throw(f"{site} is still being provisioned")

	job_id = f"saas-bridge-apps-{site}"
	state = provision.get_state(site, kind=provision.APPS_STATE)
	if (state and state.get("status") in ("queued", "running")) or is_job_enqueued(job_id):
		frappe.throw(f"App changes on {site} are already in progress")

	provision.set_state(
		site,
		kind=provision.APPS_STATE,
		status="queued",
		install=install,
		uninstall=uninstall,
		backup=backup,
		queued_at=now(),
		error=None,
		apps_installed=None,
		apps_removed=None,
		# the previous run's timestamps are still in this key, and a queued run showing when
		# some earlier one started reads as progress it has not made
		started_at=None,
		finished_at=None,
	)

	job = frappe.enqueue(
		"saas_bridge.provision.change_site_apps",
		queue="long",
		# every app is its own bench step and each may take the full step timeout
		timeout=(len(install) + len(uninstall)) * provision.step_timeout() + 60,
		job_id=job_id,
		deduplicate=True,
		site=site,
		install=install,
		uninstall=uninstall,
		backup=backup,
	)

	if job is None:
		frappe.throw(f"App changes on {site} are already in progress")

	return {
		"site": site,
		"install": install,
		"uninstall": uninstall,
		"backup": backup,
		"status": "queued",
		"job_id": job.id,
	}


@frappe.whitelist()
def get_site_apps_status(site=None):
	"""Progress of a `set_site_apps` call: queued, running, success or failed."""
	frappe.only_for("System Manager")

	site = provision.normalize_site_name(site)
	state = provision.get_state(site, kind=provision.APPS_STATE)
	if not state:
		frappe.throw(f"No app change recorded for {site}")
	return state


@frappe.whitelist()
def get_site_apps(site=None):
	"""What is installed on one site, and what else this bench could install on it."""
	frappe.only_for("System Manager")

	site = provision.ensure_site_exists(provision.normalize_site_name(site))
	installed = provision.site_apps(site)

	return {
		"site": site,
		"installed": installed,
		"available": [app for app in get_available_apps() if app not in installed],
		# the caller has to be told the difference between "no apps" and "could not look"
		"error": None if installed else f"Could not read the apps installed on {site}",
	}


@frappe.whitelist(methods=["POST"])
def drop_site(site=None, backup=1):
	"""Take a site off this bench, keeping its files in `archived/sites`.

	`bench drop-site` drops the database and the database user and moves the site
	directory into the archive. The database itself is not archived, so the backup taken
	first (`backup`, on by default) is what makes the removal reversible — pass `0` only
	when the data is genuinely disposable.

	Validated synchronously and enqueued: backing up a large site runs well past the HTTP
	timeout. Poll `get_site_drop_status`.
	"""
	frappe.only_for("System Manager")

	site = provision.ensure_site_exists(provision.normalize_site_name(site))

	# the site serving this request is the one running this code: dropping it would take
	# the database out from under the caller mid-request
	if site == frappe.local.site:
		frappe.throw(f"{site} is the site serving this request — drop it with bench")

	backup = 1 if cint(backup) else 0

	# fail here rather than in the worker, where a config gap only surfaces on polling
	provision.bench_command()
	provision.db_root_password()

	# a site still being built is being written to by another job right now, and its
	# directory may not even be complete yet
	provisioning = provision.get_state(site)
	if provisioning and provisioning.get("status") in ("queued", "running"):
		frappe.throw(f"{site} is still being provisioned")

	apps_run = provision.get_state(site, kind=provision.APPS_STATE)
	if apps_run and apps_run.get("status") in ("queued", "running"):
		frappe.throw(f"App changes on {site} are still running")

	job_id = f"saas-bridge-drop-{site}"
	state = provision.get_state(site, kind=provision.DROP_STATE)
	if (state and state.get("status") in ("queued", "running")) or is_job_enqueued(job_id):
		frappe.throw(f"{site} is already being dropped")

	provision.set_state(
		site,
		kind=provision.DROP_STATE,
		status="queued",
		backup=backup,
		queued_at=now(),
		error=None,
		# the previous run's timestamps are still in this key, and a queued run showing when
		# some earlier one started reads as progress it has not made
		started_at=None,
		finished_at=None,
		archive=None,
	)

	job = frappe.enqueue(
		"saas_bridge.provision.drop_site",
		queue="long",
		# the backup is the slow half, and it may take the full step timeout on its own
		timeout=2 * provision.step_timeout() + 60,
		job_id=job_id,
		deduplicate=True,
		site=site,
		backup=backup,
	)

	if job is None:
		frappe.throw(f"{site} is already being dropped")

	return {"site": site, "backup": backup, "status": "queued", "job_id": job.id}


@frappe.whitelist()
def get_site_drop_status(site=None):
	"""Progress of a `drop_site` call: queued, running, success or failed."""
	frappe.only_for("System Manager")

	site = provision.normalize_site_name(site)
	state = provision.get_state(site, kind=provision.DROP_STATE)
	if not state:
		frappe.throw(f"No removal recorded for {site}")
	return state


@frappe.whitelist(methods=["POST"])
def set_site_limits(site=None, max_users=None):
	"""Set the number of users a site may have.

	The value goes into the site's own `site_config.json`, where `habibi_core.limits`
	reads it: the site refuses a save that would take it past `max_users`. Enforcement
	lives on the site because that is where users are created; the number lives in the
	config because that is the one place the tenant cannot edit.

	A site with no limit set is unlimited, and `max_users: 0` is refused rather than
	treated as "none" — clearing a limit is not something to do by typo.
	"""
	frappe.only_for("System Manager")

	site = provision.ensure_site_exists(provision.normalize_site_name(site))
	values = provision.validate_limits(max_users)
	if not values:
		frappe.throw("`max_users` is required")

	provision.bench_command()

	try:
		provision.set_site_config_values(site, values)
		# without this a site that cached its hooks before the enforcing app was deployed
		# accepts the limit and goes on ignoring it, with nothing to show for it
		provision.clear_site_cache(site)
	except provision.ProvisionError as exc:
		frappe.throw(f"Could not set limits on {site}: {exc}")

	return {"site": site, **values, "enforced": provision.LIMIT_ENFORCED_BY in provision.site_apps(site)}


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
def get_sites():
	"""Sites that already exist on this bench.

	Only used to fill the site field on the desk page — every endpoint still takes a site
	name outright, so nothing depends on this list.
	"""
	frappe.only_for("System Manager")

	return provision.list_sites()


@frappe.whitelist()
def get_sites_info():
	"""Every site on this bench with what its own database says about it.

	Apps, users, languages and the site's creation date are read straight from each site's
	database rather than through `bench --site X execute`, which would cost seconds per
	site. A site that cannot be read is still listed, with the reason in `error`.
	"""
	frappe.only_for("System Manager")

	return provision.describe_sites()


@frappe.whitelist()
def get_available_apps():
	"""Apps that `create_site` will accept on this bench."""
	frappe.only_for("System Manager")

	available = frappe.get_all_apps(with_internal_apps=False)
	allowed = frappe.conf.get("saas_bridge_allowed_apps")
	return [app for app in available if app != "frappe" and (not allowed or app in allowed)]
