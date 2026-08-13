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


def validate_password(password, label):
	if not password or len(password) < MIN_PASSWORD_LENGTH:
		frappe.throw(f"{label} must be at least {MIN_PASSWORD_LENGTH} characters long")
	return password


# --- run state -------------------------------------------------------------


def state_key(site):
	return f"saas_bridge:provision:{site}"


def get_state(site):
	# expires=True: the key has a TTL, so it must not be held in frappe.local
	return frappe.cache.get_value(state_key(site), expires=True)


def set_state(site, **changes):
	state = get_state(site) or {"site": site}
	state.update(changes)
	frappe.cache.set_value(state_key(site), state, expires_in_sec=STATE_TTL)
	return state


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


def provision_site(site, apps, admin_password, email=None, password=None, first_name=None, last_name=None):
	"""Background job: create the site, install the apps, then add the login.

	Runs the two bench steps in order so a failure to add the login still leaves a
	usable site reachable with the Administrator password.
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

	except Exception as exc:
		message = scrub(str(exc), secrets)
		cleanup = drop_partial_site(site, root_password, timeout) if owns_site_dir else None
		set_state(site, status="failed", error=message, cleanup=cleanup, finished_at=now())
		frappe.log_error(title=f"SaaS Bridge: provisioning {site} failed", message=message)
		raise

	return set_state(site, status="success", finished_at=now())
