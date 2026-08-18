"""Site provisioning: shells out to `bench` to create a site and its first login.

Creating a site cannot be done in-process — `frappe.installer` mutates the global site
context of whatever process calls it, which would corrupt the worker running this code.
So each step is a `bench` subprocess launched from the bench root.

Secrets are passed as argv items because the bench CLI has no other way to accept them;
they are therefore briefly visible in `ps` on the provisioning host. They are scrubbed
out of anything that gets stored or logged.
"""

import json
import os
import re
import shutil
import subprocess

import frappe
from frappe.utils import cint, get_bench_path, now

# 253 chars is the DNS limit for a whole name
SITE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{0,252}$")
# the same pattern frappe's own Language doctype validates its code with
LANGUAGE_CODE_PATTERN = re.compile(r"^[a-zA-Z]+[-_]*[a-zA-Z]+$")
MIN_PASSWORD_LENGTH = 8
# the contract with the tenant side: this app writes this key into a site's config,
# `habibi_core.limits` reads it there and enforces the seat count. Neither app needs to
# know anything else about the other — raising a limit is a config write, not a deployment.
MAX_USERS_KEY = "saas_bridge_max_users"
# the app that enforces the limit on the site itself. A site without it takes the config
# key and ignores it, which is worth saying out loud rather than leaving to be discovered
LIMIT_ENFORCED_BY = "habibi_core"
# the two kinds of run this app keeps state for, kept apart so an app change does not
# overwrite the record of how the site was created
PROVISION_STATE = "provision"
APPS_STATE = "apps"
DEFAULT_TIMEOUT = 30 * 60
DEFAULT_COMMAND_TIMEOUT = 5 * 60
STATE_TTL = 24 * 60 * 60


class ProvisionError(Exception):
	pass


# --- request parsing -------------------------------------------------------


def as_list(value):
	"""Accept a JSON array, a comma separated string, or an already decoded list.

	Form-encoded callers cannot send arrays, so `apps=erpnext,hrms` has to work too.
	"""
	if not value:
		return []
	if isinstance(value, str):
		value = value.strip()
		value = frappe.parse_json(value) if value.startswith("[") else value.split(",")
	if not isinstance(value, list | tuple):
		frappe.throw("`apps` must be a list of app names")
	return [str(app).strip() for app in value if str(app).strip()]


def normalize_site_name(site):
	"""Lowercase and sanity check a site name.

	Rejecting anything outside the pattern is what keeps a caller-supplied name from
	reaching argv as an option or escaping the sites directory.
	"""
	site = (site or "").strip().lower().rstrip(".")
	if not site:
		frappe.throw("`site` is required")
	if not SITE_NAME_PATTERN.match(site) or ".." in site:
		frappe.throw(f"Invalid site name {site!r}: use lowercase letters, digits, dots and hyphens only")
	return site


def ensure_site_available(site):
	if os.path.exists(os.path.join(get_bench_path(), "sites", site)):
		frappe.throw(f"Site {site} already exists on this bench")
	return site


def ensure_site_exists(site):
	"""The opposite check: for calls that act on a site that must already be there."""
	if not os.path.exists(os.path.join(get_bench_path(), "sites", site)):
		frappe.throw(f"Site {site} does not exist on this bench")
	return site


def list_sites():
	"""Site names on this bench, for the desk page to offer.

	The sites directory also holds `assets`, `apps.txt` and the shared config, so a site is
	recognised by its own `site_config.json` rather than by being a directory.
	"""
	sites_path = os.path.join(get_bench_path(), "sites")
	sites = [
		entry
		for entry in os.listdir(sites_path)
		if os.path.exists(os.path.join(sites_path, entry, "site_config.json"))
	]
	return sorted(sites)


def site_config(site):
	"""A site's own `site_config.json`, or an empty dict when it cannot be read."""
	path = os.path.join(get_bench_path(), "sites", site, "site_config.json")
	try:
		with open(path) as config_file:
			return json.load(config_file)
	except (OSError, ValueError):
		return {}


def validate_language(language):
	"""Check a language code before it reaches another site's `bench execute`.

	Language records are named by their code, so this is both the record name and — since
	`bench execute` evaluates its `--kwargs` as python — the only thing standing between a
	request and code running in the target site.
	"""
	language = (language or "").strip()
	if not LANGUAGE_CODE_PATTERN.match(language):
		frappe.throw(
			f"Invalid language code {language!r}: letters only, optionally joined by a "
			f"hyphen or underscore, such as `ru` or `pt-BR`"
		)
	return language


def validate_apps(apps):
	"""Keep only apps that exist on this bench, in the order requested.

	`frappe` is dropped rather than rejected — `bench new-site` always installs it, so
	listing it explicitly is a reasonable thing for a caller to do.
	"""
	available = frappe.get_all_apps(with_internal_apps=False)
	allowed = frappe.conf.get("saas_bridge_allowed_apps") or available

	validated = []
	for app in as_list(apps):
		if app == "frappe":
			continue
		if app not in available:
			frappe.throw(f"App {app!r} is not installed on this bench")
		if app not in allowed:
			frappe.throw(f"App {app!r} is not allowed for provisioning")
		if app not in validated:
			validated.append(app)
	return validated


def validate_app_changes(site, install=None, uninstall=None):
	"""Check a set of app changes against what the site actually has installed.

	Both lists are checked before either runs, so a request that asks for one impossible
	change makes none of them rather than half of them.

	The allowed-apps list is only applied to installs: an app that was once allowed and no
	longer is still has to be removable, and removing it is how a site gets back into line.

	Nothing here has to rule out an app appearing in both lists — one requires it to be
	absent from the site, the other requires it to be there.
	"""
	installed = site_apps(site)
	# every site has frappe installed, so an empty list is never a site without apps — it is
	# `site_apps` swallowing the error from a database it could not read
	if not installed:
		frappe.throw(f"Could not read the apps installed on {site} — is its database reachable?")

	install = validate_apps(install)

	validated_uninstall = []
	for app in as_list(uninstall):
		if app == "frappe":
			frappe.throw("`frappe` cannot be uninstalled — drop the site instead")
		if app not in installed:
			frappe.throw(f"App {app!r} is not installed on {site}")
		if app not in validated_uninstall:
			validated_uninstall.append(app)

	already = [app for app in install if app in installed]
	if already:
		frappe.throw(f"Already installed on {site}: {', '.join(already)}")

	if not install and not validated_uninstall:
		frappe.throw("Nothing to do — pass `install`, `uninstall` or both")

	return install, validated_uninstall


def validate_limits(max_users=None):
	"""Check the limit keys before they are written into a site's config.

	Returns a dict rather than a number: it is what `set_site_config_values` writes, and
	the day a second limit exists it lands here without changing its callers.
	"""
	values = {}

	if max_users not in (None, ""):
		max_users = cint(max_users)
		# a site always holds Administrator, so a limit below one could never be satisfied
		if max_users < 1:
			frappe.throw("`max_users` must be at least 1")
		values[MAX_USERS_KEY] = max_users

	return values


def validate_password(password, label):
	if not password or len(password) < MIN_PASSWORD_LENGTH:
		frappe.throw(f"{label} must be at least {MIN_PASSWORD_LENGTH} characters long")
	return password


# --- run state -------------------------------------------------------------


def state_key(site, kind=PROVISION_STATE):
	return f"saas_bridge:{kind}:{site}"


def get_state(site, kind=PROVISION_STATE):
	# expires=True: the key has a TTL, so it must not be held in frappe.local
	return frappe.cache.get_value(state_key(site, kind), expires=True)


def set_state(site, kind=PROVISION_STATE, **changes):
	state = get_state(site, kind) or {"site": site}
	state.update(changes)
	frappe.cache.set_value(state_key(site, kind), state, expires_in_sec=STATE_TTL)
	return state


# --- reading other sites ---------------------------------------------------


def site_database(conf):
	"""Open a connection to another site's database, using that site's own credentials.

	Reading a site this way rather than through `bench --site X execute` is what makes a
	list of every site on the bench affordable: a bench subprocess costs seconds per site,
	a connection costs milliseconds. It is only ever used for the read-only queries below —
	anything that writes still goes through bench, where frappe's own document machinery
	runs.

	The connection is deliberately separate from `frappe.local.db`: the current request
	keeps its own site's connection untouched.
	"""
	from frappe.database import get_db

	db = get_db(
		socket=conf.get("db_socket") or frappe.conf.db_socket,
		host=conf.get("db_host") or frappe.conf.db_host,
		port=conf.get("db_port") or frappe.conf.db_port,
		user=conf.get("db_name"),
		password=conf.get("db_password"),
		cur_db_name=conf.get("db_name"),
	)
	db.connect()
	return db


def set_site_config_values(site, values):
	"""Write keys into another site's `site_config.json` via `bench set-config`.

	Written from here rather than into a doctype on the site itself: the tenant's own
	System Manager can edit anything inside their database, permissions included, but has
	no way to reach this file. A limit that the limited party can raise is not a limit.

	`--parse` makes bench store a number as a number, so a caller reading the config back
	gets `10` rather than `"10"`.
	"""
	for key, value in values.items():
		args = [bench_command(), "--site", site, "set-config", key, str(value)]
		if isinstance(value, int) and not isinstance(value, bool):
			args.append("--parse")
		run(args, f"`bench --site {site} set-config {key}`", [], command_timeout())

	return values


def clear_site_cache(site):
	"""Drop the target site's caches, hooks included.

	Frappe keeps each site's resolved `hooks.py` in redis, and a site that cached them
	before the enforcing app was deployed goes on running without its hooks until
	something clears it. Setting a limit and having the site quietly ignore it is the one
	failure here that gives no sign at all, so the write pays for the extra second.
	"""
	run(
		[bench_command(), "--site", site, "clear-cache"],
		f"`bench --site {site} clear-cache`",
		[],
		command_timeout(),
	)


def read_installed_apps(db):
	"""The apps a site actually runs, read from the same place frappe reads them.

	`frappe.get_installed_apps` reads this global, and everything that follows from it does
	too: which hooks resolve, and whether `bench install-app` / `uninstall-app` think there
	is anything to do. `tabInstalled Application` is a display table kept alongside it, and
	the two can drift — checking an app change against the table would then report a removal
	that bench had quietly refused as done.
	"""
	row = db.sql(
		"select defvalue from tabDefaultValue where parent = '__global' and defkey = 'installed_apps'"
	)
	return frappe.parse_json(row[0][0]) if row and row[0][0] else []


def read_site_details(conf):
	"""What a site's own database says about it."""
	db = site_database(conf)
	try:
		return {
			"apps": read_installed_apps(db),
			# Administrator counts: it is the login the site was created with, and leaving it
			# out reports a brand new site as having no users at all. Guest never does — it
			# is a fixture, not an account anyone holds
			"users": db.sql(
				"select count(*) from `tabUser` where enabled = 1 and user_type = 'System User' and name != 'Guest'"
			)[0][0],
			"website_users": db.sql(
				"select count(*) from `tabUser` where enabled = 1 and user_type = 'Website User' and name != 'Guest'"
			)[0][0],
			"disabled_users": db.sql(
				"select count(*) from `tabUser` where enabled = 0 and name != 'Guest'"
			)[0][0],
			"user_list": db.sql(
				"""select name, full_name, last_active from `tabUser`
				where enabled = 1 and user_type = 'System User' and name != 'Guest'
				order by creation limit 20""",
				as_dict=True,
			),
			# the install stamps Administrator when the site is built, which is the closest
			# thing a site has to its own creation date
			"created": db.sql("select creation from `tabUser` where name = 'Administrator'")[0][0],
			"default_language": (
				db.sql("select value from `tabSingles` where doctype = 'System Settings' and field = 'language'")
				or [[None]]
			)[0][0],
			"enabled_languages": [
				row[0] for row in db.sql("select name from `tabLanguage` where enabled = 1 order by name")
			],
		}
	finally:
		db.close()


def site_apps(site):
	"""Apps installed on another site, or an empty list when it cannot be read.

	Its own query rather than `read_site_details`, which asks four more things a caller
	checking for one app has no use for.
	"""
	try:
		db = site_database(site_config(site))
	except Exception:
		return []

	try:
		return read_installed_apps(db)
	finally:
		db.close()


def describe_site(site):
	"""Everything the desk page shows about one site.

	A site that cannot be read is still listed, with the reason in `error` — one broken
	site must not blank out the whole list.
	"""
	conf = site_config(site)
	details = {
		"site": site,
		"db_name": conf.get("db_name"),
		"maintenance_mode": cint(conf.get("maintenance_mode")),
		"scheduler_paused": cint(conf.get("pause_scheduler")),
		# the limit this app writes and habibi_core enforces on the site itself
		"max_users": cint(conf.get(MAX_USERS_KEY)) or None,
		"last_run": get_state(site),
		"last_apps_run": get_state(site, kind=APPS_STATE),
		"error": None,
	}

	try:
		details.update(read_site_details(conf))
	except Exception as exc:
		details["error"] = str(exc)

	# None rather than False when the site could not be read: not knowing whether a limit
	# is enforced is a different thing from knowing that it is not
	details["limit_enforced"] = (
		None if details.get("apps") is None else LIMIT_ENFORCED_BY in details["apps"]
	)

	return details


def describe_sites():
	return [describe_site(site) for site in list_sites()]


# --- bench plumbing --------------------------------------------------------


def bench_command():
	configured = frappe.conf.get("saas_bridge_bench_command")
	if configured:
		return configured

	# the worker's PATH often lacks the bench entrypoint, so prefer the bench's own venv
	in_venv = os.path.join(get_bench_path(), "env", "bin", "bench")
	if os.path.exists(in_venv):
		return in_venv

	found = shutil.which("bench")
	if not found:
		frappe.throw("`bench` executable not found — set `saas_bridge_bench_command` in site config")
	return found


def step_timeout():
	"""Seconds a single bench step may take before it is killed."""
	return cint(frappe.conf.get("saas_bridge_provision_timeout")) or DEFAULT_TIMEOUT


def command_timeout():
	"""Seconds a short `bench --site ... execute` may take before it is killed.

	Much shorter than `step_timeout`: these calls run inside a web request, and all they do
	is boot a site and write one row.
	"""
	return cint(frappe.conf.get("saas_bridge_site_command_timeout")) or DEFAULT_COMMAND_TIMEOUT


def db_root_password():
	for key in ("saas_bridge_db_root_password", "db_root_password", "root_password"):
		if frappe.conf.get(key):
			return frappe.conf.get(key)
	frappe.throw("Database root password missing — set `saas_bridge_db_root_password` in site config")


def extra_new_site_args():
	"""Bench specific flags to append to `new-site`.

	Docker benches need `--mariadb-user-host-login-scope=%` here, otherwise the database
	user is scoped to one host and the new site cannot reach its own database. Read from
	config only — never from the request.
	"""
	args = frappe.conf.get("saas_bridge_new_site_extra_args") or []
	if isinstance(args, str):
		args = args.split()
	return [str(arg) for arg in args]


def scrub(text, secrets):
	for secret in secrets:
		if secret:
			text = text.replace(secret, "***")
	return text


def drop_partial_site(site, root_password, timeout):
	"""Archive the half-built site a failed `new-site` leaves behind.

	Without this the directory survives the failure and the name can never be retried —
	the next attempt is refused by the "already exists" check. Only ever reached after
	`ensure_site_available` passed inside the same job, so the directory can only have
	come from the run that just failed, never from an existing tenant.

	`bench drop-site` moves the site to `archived/sites` rather than deleting it, so a
	failed run stays inspectable.
	"""
	if not os.path.exists(os.path.join(get_bench_path(), "sites", site)):
		return None
	try:
		args = [
			bench_command(),
			"drop-site",
			site,
			"--db-root-password",
			root_password,
			"--force",
			"--no-backup",
		]
		run(args, f"`bench drop-site {site}`", [root_password], timeout)
		return "archived"
	except Exception as exc:
		frappe.log_error(
			title=f"SaaS Bridge: `bench drop-site {site}` failed",
			message=scrub(str(exc), [root_password]),
		)

	# drop-site needs the database root password too, so the one failure that matters most
	# here — a wrong root password — breaks the install and the drop alike. Move the
	# directory aside directly so the name is still retriable.
	try:
		return archive_site_directory(site)
	except Exception as exc:
		frappe.log_error(title=f"SaaS Bridge: cleanup of {site} failed", message=str(exc))
		return "failed"


def archive_site_directory(site):
	"""Move a half-built site directory into `archived/sites`.

	Moved rather than deleted: the caller only knows the run failed, not what `new-site`
	managed to write, and a move can never destroy a working site.
	"""
	source = os.path.join(get_bench_path(), "sites", site)
	if not os.path.exists(source):
		return None

	archive_dir = os.path.join(get_bench_path(), "archived", "sites")
	os.makedirs(archive_dir, exist_ok=True)

	target = os.path.join(archive_dir, site)
	suffix = 1
	while os.path.exists(target):
		target = os.path.join(archive_dir, f"{site}-{suffix}")
		suffix += 1

	shutil.move(source, target)
	return "archived"


def run(args, label, secrets, timeout):
	"""Run one bench command, raising ProvisionError with scrubbed output on failure."""
	try:
		result = subprocess.run(
			args,
			cwd=get_bench_path(),
			capture_output=True,
			text=True,
			timeout=timeout,
			check=False,
		)
	except subprocess.TimeoutExpired:
		raise ProvisionError(f"{label} timed out after {timeout}s")

	if result.returncode:
		detail = (result.stderr or result.stdout or "").strip()[-2000:]
		raise ProvisionError(scrub(detail, secrets) or f"bench exited with code {result.returncode}")

	return scrub(result.stdout or "", secrets)


def execute_on_site(site, method, kwargs=None, label=None):
	"""Call a python method inside another site's context, via `bench --site X execute`.

	It has to be a subprocess. `frappe.init(other_site)` would rebind the site globals of
	the process that calls it, which in a web request means tearing the current request's
	own site context out from under it. A `bench` child gets its own globals and its own
	database connection, and `bench execute` commits before it exits.

	Only methods of apps installed on the *target* site can be run this way, so the method
	is a frappe one rather than something out of this app: `frappe.get_attr` refuses
	anything else.
	"""
	args = [bench_command(), "--site", site, "execute", method]
	if kwargs:
		# bench evaluates this as python, which json.dumps output is a valid subset of as
		# long as no value is a bool or None — keep the callers passing ints and strings
		args += ["--kwargs", json.dumps(kwargs)]

	return run(args, label or f"`bench --site {site} execute {method}`", [], command_timeout())


# --- the job ---------------------------------------------------------------


def provision_site(
	site,
	apps,
	admin_password,
	email=None,
	password=None,
	first_name=None,
	last_name=None,
	limits=None,
):
	"""Background job: create the site, install the apps, then add the login.

	Runs the bench steps in order so a failure to add the login still leaves a usable site
	reachable with the Administrator password.
	"""
	timeout = step_timeout()
	secrets = [admin_password, password]
	root_password = None
	owns_site_dir = False

	# state goes to `running` before anything that can fail, so a misconfigured bench is
	# reported as a failure instead of leaving the run stuck on `queued` forever
	set_state(site, status="running", started_at=now(), error=None)

	try:
		root_password = db_root_password()
		secrets.append(root_password)

		# re-checked in the worker, not just in the API: passing here is what proves any
		# directory at this path afterwards belongs to this run and may be cleaned up
		ensure_site_available(site)
		owns_site_dir = True

		new_site = [
			bench_command(),
			"new-site",
			site,
			"--db-root-password",
			root_password,
			"--admin-password",
			admin_password,
		]
		for app in apps:
			new_site += ["--install-app", app]
		new_site += extra_new_site_args()

		run(new_site, f"`bench new-site {site}`", secrets, timeout)
		set_state(site, apps_installed=apps)

		if email:
			add_login = [bench_command(), "--site", site, "add-system-manager", email]
			if password:
				add_login += ["--password", password]
			if first_name:
				add_login += ["--first-name", first_name]
			if last_name:
				add_login += ["--last-name", last_name]

			run(add_login, f"`bench add-system-manager {email}`", secrets, timeout)
			set_state(site, login_created=email)

		# written last, after the site's own first login exists: a limit of one user would
		# otherwise have the new site refuse the login this very job is adding
		if limits:
			set_site_config_values(site, limits)
			set_state(site, limits=limits)

	except Exception as exc:
		message = scrub(str(exc), secrets)
		cleanup = drop_partial_site(site, root_password, timeout) if owns_site_dir else None
		set_state(site, status="failed", error=message, cleanup=cleanup, finished_at=now())
		frappe.log_error(title=f"SaaS Bridge: provisioning {site} failed", message=message)
		raise

	return set_state(site, status="success", finished_at=now())


def change_site_apps(site, install=None, uninstall=None, backup=1):
	"""Background job: uninstall apps from an existing site, then install others.

	A job rather than a request for the same reason as `provision_site`: installing an app
	migrates the whole site and takes minutes. Poll the `apps` run state for the outcome.

	Removals run first, so swapping one app for another that conflicts with it works in a
	single call.

	Every step is verified against the site's own list of installed apps afterwards. It has
	to be: `bench uninstall-app` prints "X is a dependency of Y" and exits **zero** without
	removing anything, and `install-app` does the same for an app that is already there.
	Trusting the exit code alone would report those as done.
	"""
	install = install or []
	uninstall = uninstall or []
	timeout = step_timeout()
	removed = []
	installed = []

	set_state(
		site,
		kind=APPS_STATE,
		status="running",
		started_at=now(),
		error=None,
		install=install,
		uninstall=uninstall,
		apps_installed=[],
		apps_removed=[],
		finished_at=None,
	)

	try:
		for app in uninstall:
			args = [bench_command(), "--site", site, "uninstall-app", app, "--yes"]
			# the backup is the only way back from a removal that took a tenant's data with
			# it, so it is opt-out rather than opt-in
			if not cint(backup):
				args.append("--no-backup")

			output = run(args, f"`bench --site {site} uninstall-app {app}`", [], timeout)
			if app in site_apps(site):
				raise ProvisionError(
					f"{app} is still installed on {site} after uninstall-app — "
					f"{output.strip()[-500:] or 'bench reported nothing'}"
				)

			removed.append(app)
			set_state(site, kind=APPS_STATE, apps_removed=list(removed))

		for app in install:
			output = run(
				[bench_command(), "--site", site, "install-app", app],
				f"`bench --site {site} install-app {app}`",
				[],
				timeout,
			)
			if app not in site_apps(site):
				raise ProvisionError(
					f"{app} is not installed on {site} after install-app — "
					f"{output.strip()[-500:] or 'bench reported nothing'}"
				)

			installed.append(app)
			set_state(site, kind=APPS_STATE, apps_installed=list(installed))

		# the target site keeps its resolved hooks and its app list in redis, and both just
		# changed — without this it goes on serving the old set until something else clears it
		clear_site_cache(site)

	except Exception as exc:
		message = str(exc)
		set_state(site, kind=APPS_STATE, status="failed", error=message, finished_at=now())
		frappe.log_error(title=f"SaaS Bridge: changing apps on {site} failed", message=message)
		raise

	return set_state(site, kind=APPS_STATE, status="success", finished_at=now())
