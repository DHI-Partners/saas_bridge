### SaaS Bridge

External role and subscription control API

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch version-16
bench install-app saas_bridge
```

### Desk interface

Installing the app puts a **SaaS Bridge** workspace in the sidebar with a **Site Manager**
page (`/app/site-manager`) behind it:

- **Sites** — every site on the bench with its apps, users, default language, creation
  date and any maintenance, scheduler or failed-run flags. A row opens for the logins
  themselves and when they were last active, the full app and language lists, the database
  name and the last provisioning run; **Pick** on a row fills the forms below it with that
  site and its current limit. A site whose database cannot be read is still listed, marked
  `unreachable` with the reason in its detail.
- **Create a site** — name, apps, and an optional System Manager login. Passwords left
  empty are generated and shown once, and the run's progress is polled below the form
  until it succeeds or fails.
- **Site apps** — adds and removes apps on a site that already exists. The pickers only
  offer what the picked site can take: apps it does not have on the install side, apps it
  does have on the uninstall side. Removals are confirmed by name before anything runs, and
  the run's progress is polled below the form.
- **User limit** — writes the seat limit into a site's own config.
- **Site language** — enables a language on a site that already exists, Russian by
  default.

All of them are limited to the `System Manager` role, the same as the endpoints they call. Run
`bench --site <control-site> migrate` after installing or updating the app, otherwise the
page and the workspace are not registered on the site yet.

### Site provisioning API

Creates a new site on the same bench with the apps given in the request, an Administrator
password and a System Manager login.

Requires the `System Manager` role. Add the database root password to the controlling
site's `site_config.json` first:

```json
{
	"saas_bridge_db_root_password": "...",
	"saas_bridge_allowed_apps": ["erpnext", "hrms"],
	"saas_bridge_new_site_extra_args": ["--mariadb-user-host-login-scope=%"],
	"saas_bridge_provision_timeout": 1800,
	"saas_bridge_bench_command": "/home/frappe/frappe-bench/env/bin/bench"
}
```

Only `saas_bridge_db_root_password` is required. `saas_bridge_allowed_apps` restricts what
callers may install (default: every app on the bench). `saas_bridge_new_site_extra_args` is passed
through to `bench new-site` — a docker bench needs
`--mariadb-user-host-login-scope=%` here, or the new site cannot reach its own database.
`saas_bridge_bench_command` is only needed when `bench` is neither in the bench's own venv
nor on the worker's `PATH`.

#### Setting it up on a docker bench

`bench` lives inside the backend container, not on the host, so the two keys are set
through it. Substitute the compose service name if yours is not `backend`:

```bash
docker compose exec backend bench --site <control-site> \
	set-config saas_bridge_db_root_password '<mariadb root password>'

docker compose exec backend bench --site <control-site> \
	set-config saas_bridge_new_site_extra_args '["--mariadb-user-host-login-scope=%"]' --parse

docker compose exec backend bench --site <control-site> \
	execute "frappe.conf.get('saas_bridge_new_site_extra_args')"
```

The last line is the check: it must print the list back. Both keys belong to the
**controlling** site — that is the config `create_site` reads. No restart is needed, the
config is read per request. `--parse` is what stores a list as a list rather than as a
string, and the same flag is what stores `max_users` as a number.

Set both **before** creating any site. `saas_bridge_new_site_extra_args` only affects the
moment of creation and does not retrofit sites that already exist. Two failures follow from
skipping them, and they look nothing alike:

| What you see | What it means |
| --- | --- |
| `Database root password missing — set saas_bridge_db_root_password in site config` on **Create site** | the first key is not set. Nothing was created; the check runs before the job is queued. |
| The run reports `success`, but the site shows up `unreachable` with `(1045, "Access denied for user '_xxx'@'172.18.0.7'")` — and answers HTTP with a 500 | created without `--mariadb-user-host-login-scope=%`. Its database user is bound to a single host and the backend is not it. |

The second one has to be fixed per site: set the key, then drop and recreate the site, or
grant the existing user access from any host. Note that binding the user to the backend's
current address instead of `%` is not a fix — container addresses change on the next
`docker compose up`, and the site breaks again.

```bash
docker compose exec backend bash -lc \
	'bench drop-site <site> \
		--db-root-password "$(bench --site <control-site> execute "frappe.conf.saas_bridge_db_root_password")" \
		--force --no-backup'
```

That reads the password from the config inside the container, so it reaches neither the
terminal nor the shell history. `drop-site` drops the database and moves the site directory
to `archived/sites` rather than deleting it, which on a containerised bench means inside
the container that ran it.

**`POST /api/method/saas_bridge.api.create_site`**

```bash
curl -X POST https://control.example.com/api/method/saas_bridge.api.create_site \
	-H "Authorization: token API_KEY:API_SECRET" \
	-H "Content-Type: application/json" \
	-d '{
		"site": "client1.example.com",
		"apps": ["erpnext", "hrms"],
		"admin_password": "s3cret-admin",
		"email": "owner@client1.com",
		"password": "s3cret-login",
		"first_name": "Owner"
	}'
```

| Field | Required | Description |
| --- | --- | --- |
| `site` | yes | Full site name, lowercase. |
| `apps` | no | Apps to install, as a JSON array or a comma separated string. |
| `admin_password` | no | Administrator password. Generated and returned if omitted. |
| `email` | no | Login to create as System Manager on the new site. |
| `password` | no | Password for that login. Generated and returned if omitted. |
| `first_name`, `last_name` | no | Name for that login. Defaults to the local part of the email. |
| `max_users` | no | Seat limit, written to the new site's config once it is built. See `set_site_limits`. |

The request is validated synchronously and the install is enqueued, since `bench new-site`
with a few apps runs well past the HTTP timeout:

```json
{"message": {"site": "client1.example.com", "apps": ["erpnext", "hrms"],
	"login": "owner@client1.com", "status": "queued", "job_id": "..."}}
```

Generated passwords are returned in this response only — they are never stored, so this is
the caller's one chance to keep them.

**`GET /api/method/saas_bridge.api.get_site_status?site=client1.example.com`**

Polls the run: `queued`, `running`, `success` or `failed`. A failed run carries the bench error in `error`, with passwords
scrubbed out. Run state is kept for 24 hours.

A failed `bench new-site` leaves a half-built site directory behind, which would block
every later attempt at that name. The failure path therefore moves it into
`archived/sites` and reports the outcome in `cleanup` (`archived`, `failed` or `null`), so
the same name can simply be retried. Only a directory this run created is ever touched.
On a containerised bench, note that `archived/` is usually not on the shared sites volume,
so those archives live inside the container that did the work.

**`POST /api/method/saas_bridge.api.set_site_apps`**

Adds and removes apps on a site that already exists.

```bash
curl -X POST https://control.example.com/api/method/saas_bridge.api.set_site_apps \
	-H "Authorization: token API_KEY:API_SECRET" \
	-H "Content-Type: application/json" \
	-d '{"site": "client1.example.com", "install": ["hrms"], "uninstall": ["habibi_telegram"]}'
```

```json
{"message": {"site": "client1.example.com", "install": ["hrms"],
	"uninstall": ["habibi_telegram"], "backup": 1, "status": "queued", "job_id": "..."}}
```

| Field | Required | Description |
| --- | --- | --- |
| `site` | yes | The site to change. Must already exist. |
| `install` | no | Apps to install. Same list and same `saas_bridge_allowed_apps` filter as `create_site`. |
| `uninstall` | no | Apps to remove, with their doctypes and every document in them. |
| `backup` | no | `1` (default) backs the site up before the first removal, `0` skips it. |

One endpoint for both directions because they are one intent — a plan change is usually a
swap — and doing it in a single run means the two halves cannot interleave with another
request's. Removals run first, so swapping one app for another that conflicts with it works
in a single call. At least one of the two lists has to be non-empty.

Both lists are checked against the site's own installed apps before either runs: an install
of an app the site already has, or a removal of one it does not, is refused outright rather
than half-applied. `frappe` cannot be uninstalled — drop the site instead — and neither can
anything be removed from the site serving the request, which would pull the desk apart
under the caller. An app that declares `required_apps` brings them with it, so a site can
end up with more apps than were asked for.

Like `create_site` this is enqueued: installing an app migrates the whole site, well past
the HTTP timeout. Poll:

**`GET /api/method/saas_bridge.api.get_site_apps_status?site=client1.example.com`**

`queued`, `running`, `success` or `failed`, with `apps_removed` and `apps_installed`
growing as the run goes and the bench error in `error` if it stops. The two lists are what
actually happened, `install` and `uninstall` what was asked for — on a failed run they
differ, and the difference is what is left to redo. Kept for 24 hours, separately from the
provisioning state, so an app change never overwrites the record of how the site was made.

Every step is verified against the site's own app list afterwards, because bench does not
fail loudly here: `uninstall-app` prints `App X is a dependency of Y. Uninstall Y first.`
and exits **zero** without removing anything, and `install-app` does the same for an app
that is already there. Taking the exit code at its word would report those as done.

**`GET /api/method/saas_bridge.api.get_site_apps?site=client1.example.com`**

What one site has installed and what else this bench could install on it:

```json
{"message": {"site": "client1.example.com", "installed": ["frappe", "erpnext"],
	"available": ["hrms"], "error": null}}
```

**`POST /api/method/saas_bridge.api.set_site_limits`**

Sets the number of users a site may have.

```bash
curl -X POST https://control.example.com/api/method/saas_bridge.api.set_site_limits \
	-H "Authorization: token API_KEY:API_SECRET" \
	-H "Content-Type: application/json" \
	-d '{"site": "client1.example.com", "max_users": 10}'
```

| Field | Required | Description |
| --- | --- | --- |
| `site` | yes | The site to limit. |
| `max_users` | yes | Enabled system users the site may have, `Administrator` included. |

The value is written into the site's own `site_config.json` as
`saas_bridge_max_users`. Enforcement is not here — it lives in
[habibi_core](https://github.com/DHI-Partners/habibi-core), which hooks `User.validate` on
the site and refuses a save that would take it past the limit.

That split is deliberate. The limit has to be written where the limited party cannot reach
it: a tenant's System Manager can edit any document in their own database, and the
permissions on it, but not a file on the bench. And the site is given a number rather than
a plan, so wherever plans end up being kept — here, or a billing service later — nothing
on the site has to change.

`create_site` takes the same `max_users`, applying it once the site and its first login
exist: a limit of one user would otherwise have the new site refuse the very login the
provisioning job is adding. `max_users` below 1 is refused — a site always holds
`Administrator`, so such a limit could never be satisfied.

The response reports `enforced`, and the Sites table marks a site `limit not enforced`,
when the target site has no `habibi_core` on it: the key is written, nothing reads it, and
the limit is decoration. Setting a limit also clears the target site's cache. Frappe keeps
each site's resolved hooks in redis, so a site that cached them before `habibi_core` was
deployed goes on accepting users past its limit with nothing to show for it — which is the
one failure here that looks exactly like success.

**`POST /api/method/saas_bridge.api.set_site_language`**

Enables a language on a site of this bench — Russian unless another one is asked for.

```bash
curl -X POST https://control.example.com/api/method/saas_bridge.api.set_site_language \
	-H "Authorization: token API_KEY:API_SECRET" \
	-H "Content-Type: application/json" \
	-d '{"site": "client1.example.com", "language": "ru"}'
```

```json
{"message": {"site": "client1.example.com", "language": "ru", "enabled": 1}}
```

| Field | Required | Description |
| --- | --- | --- |
| `site` | yes | The site to act on. |
| `language` | no | Language code, defaults to `ru`. Must already exist on the target site. |
| `enabled` | no | `1` to enable (default), `0` to disable. |

The site must already exist. Unlike `create_site` this runs synchronously — it is one row
written on a live site, a few seconds — so the response reports the finished state rather
than a job to poll. `saas_bridge_site_command_timeout` (default 300 seconds) caps it.

**`GET /api/method/saas_bridge.api.get_available_apps`**

Lists the apps `create_site` will accept on this bench.

**`GET /api/method/saas_bridge.api.get_sites`**

Lists the names of the sites that already exist on this bench.

**`GET /api/method/saas_bridge.api.get_sites_info`**

The same sites, each with what its own database says about it — this is what the Sites
table on the desk page shows:

```json
{"message": [{"site": "client1.example.com", "db_name": "_946076741d067568",
	"apps": ["frappe", "erpnext"], "created": "2026-08-13 17:51:47.395286",
	"max_users": 10,
	"users": 3, "website_users": 140, "disabled_users": 1,
	"user_list": [{"name": "owner@client1.com", "full_name": "Owner", "last_active": "..."}],
	"default_language": "ru", "enabled_languages": ["en", "ru"],
	"maintenance_mode": 0, "scheduler_paused": 0, "last_run": null, "error": null}]}
```

`users` counts enabled System Users, `Administrator` included and `Guest` excluded — a
freshly created site therefore reports one user rather than none. `user_list` holds the
first twenty of them.

Apps, users and languages are read by connecting to each site's database with the
credentials from its own `site_config.json`, not by running `bench --site X` per site: a
bench subprocess costs seconds per site, a connection costs milliseconds. The connection is
read-only and separate from the request's own — every write still goes through bench.

A site that cannot be read keeps its row, with the reason in `error` and the database
fields missing. `last_run` carries the `get_site_status` state when the site was
provisioned through this app within the last 24 hours, and `last_apps_run` the
`get_site_apps_status` state when its apps were changed through it.

`apps` is read from the site's `installed_apps` global — the same list `frappe.get_installed_apps`
returns, and the one that decides which hooks resolve — rather than from the
`Installed Application` table beside it. The two can drift, and it is the global that bench
acts on.

Note that the new site still needs to be routable. On a containerised bench the frontend
routes by the `Host` header, so a new tenant needs DNS pointing at the host — a wildcard
record on the provisioning domain, with a certificate to match — and nothing further from
this app. On a plain bench, run `bench setup nginx` and point DNS at the host, or the site
will only answer on the bench's own port.

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/saas_bridge
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit
