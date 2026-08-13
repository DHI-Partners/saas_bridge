### SaaS Bridge

External role and subscription control API

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch version-16
bench install-app saas_bridge
```

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

Note that the new site still needs to be routable — run `bench setup nginx` and point DNS
at the host, or the site will only answer on the bench's own port.

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
