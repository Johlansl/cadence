# Cadence

Lightweight patch management for Linux servers. See `CLAUDE.md` for the full
V1 brief, scope and architecture decisions.

## CI

`.gitlab-ci.yml` runs on every push: **agent** (`go vet` + `go test` + build),
**backend** (`pytest` against a `postgres:16` service), **frontend**
(`npm ci` + `npm run build`).

## Status

Incremental build, one testable step at a time (`CLAUDE.md` section 8).

- [x] **Step 1 — database scaffolding**: `docker-compose.yml` (db service only);
      schema created by Alembic from the backend entrypoint (`app.prestart`).
- [x] **Step 2 — backend (FastAPI)**: `GET /healthz`,
      `POST /api/v1/admin/hosts`, `POST /api/v1/reports`.
- [x] **Step 3 — backend read views**: `GET /api/v1/hosts`,
      `GET /api/v1/hosts/{id}`.
- [x] **Step 4 — Go agent**: collect (dpkg / apt / os-release) + report.
- [x] **Step 5 — systemd unit + timer** for the agent.
- [x] **Step 6 — frontend**: master-detail dashboard (React + Vite + Tailwind).
- [x] **Step 7 — end-to-end validation**: agent on a real Debian host,
      collection + report verified against the live stack.
- [x] **Step 8 — apply updates (jobs)**: trigger endpoint + report piggyback,
      agent runs `apt dist-upgrade`, dashboard trigger button + job log.
- [x] **Post-V1** — 1-minute job poll (`cadence-agent-poll.timer` +
      `POST /api/v1/agent/next-job`) so a triggered upgrade starts within a minute.

## Real-VM test setup

Target topology: one **central server VM** running `db` + `backend` +
`scheduler` + `frontend` + `caddy` via docker-compose, and one or more
**monitored Debian VMs** each running the agent on a systemd timer. The agent
only talks outbound to the server.

### 1. Central server VM

```sh
git clone <repo> cadence && cd cadence
cp .env.example .env
```

Edit `.env`:

| Key | Set to |
|---|---|
| `POSTGRES_PASSWORD` | a real password |
| `CADENCE_ADMIN_KEY` | `openssl rand -hex 32` |
| `CADENCE_SITE_ADDRESS` | a hostname that resolves to this server on your LAN, e.g. `cadence.lan` |

```sh
docker compose up -d --build
curl -s http://127.0.0.1:8000/healthz            # {"status":"ok"} (backend, loopback)
# dashboard: https://cadence.lan/
```

Traffic goes through the **Caddy** reverse proxy on 80/443; 80 redirects to
443. `backend` (8000) and the plain-HTTP `frontend` (8080) stay bound to
loopback — set `CADENCE_BACKEND_BIND` / `CADENCE_FRONTEND_BIND` to `0.0.0.0`
only for TLS-less debugging. `restart: unless-stopped` brings everything back
after a reboot.

### TLS — trust Caddy's internal CA on the monitored VMs

Caddy issues the `CADENCE_SITE_ADDRESS` certificate from its own CA. The Go
agent verifies against the system trust store, so each monitored VM (and any
browser) must trust that CA once:

```sh
# on the server, once the stack is up
docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt \
  > cadence-caddy-ca.crt

# copy to each monitored VM, then:
sudo cp cadence-caddy-ca.crt /usr/local/share/ca-certificates/
sudo update-ca-certificates
```

`CADENCE_SITE_ADDRESS` must also resolve on the monitored VMs (LAN DNS, or an
`/etc/hosts` entry pointing at the server).

### 2. Register each monitored VM (run on the server, or anywhere with the admin key)

```sh
scripts/provision-host.sh vm-web-01 "web frontend"
# creates the host and prints a ready-to-paste /etc/cadence/agent.env block.
# Reads CADENCE_ADMIN_KEY from .env; talks to http://127.0.0.1:8000 by default
# (override with CADENCE_API / CADENCE_AGENT_URL).
```

Or by hand:

```sh
curl -s -X POST https://cadence.lan/api/v1/admin/hosts \
  -H "X-Admin-Key: $CADENCE_ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"hostname":"vm-web-01","description":"web frontend"}'
# -> {"id":"...","hostname":"vm-web-01","token":"..."}   keep the token
```

### 3. On each monitored VM

**One-liner (recommended).** Once per server, stage the bootstrap assets:

```sh
scripts/publish-agent.sh          # builds the agent, writes ./dist (served by Caddy)
```

Then on the VM, as root — `provision-host.sh` prints this exact line:

```sh
curl -fsSL http://cadence.lan/install.sh | sudo CADENCE_TOKEN=<token> sh
```

It trusts the internal CA, makes sure a reboot-required helper is present
(`update-notifier-common`, or `reboot-notifier` on Debian 13 where the former
was dropped), drops the binary + systemd units, writes `/etc/cadence/agent.env`,
and enables the timers. The installer is served over plain HTTP (the VM does not
trust the CA yet); the binary is checksum-verified.

**By hand.** Trust the Caddy CA (see "TLS" above), then build the (static)
binary — on the VM if Go is available, or once elsewhere and `scp`
`bin/cadence-agent` + the `agent/systemd/` directory over:

```sh
cd agent
CGO_ENABLED=0 go build -trimpath -o bin/cadence-agent ./cmd/agent
sudo systemd/install.sh
sudo editor /etc/cadence/agent.env      # CADENCE_SERVER_URL=https://cadence.lan , CADENCE_TOKEN=<token>
```

Verify:

```sh
sudo systemctl start cadence-agent.service
journalctl -u cadence-agent -n 20 --no-pager         # "report sent to ..."
systemctl list-timers cadence-agent.timer            # next scheduled run
curl -s https://cadence.lan/api/v1/hosts             # the VM shows up, last_seen_at fresh
```

### 4. Upgrade an existing monitored VM

For a VM already running an older agent (e.g. 0.2.0, no poll timer, plain-HTTP
server URL). Run on the VM:

```sh
# 1. trust the Caddy CA (from the server: docker compose exec caddy \
#    cat /data/caddy/pki/authorities/local/root.crt > cadence-caddy-ca.crt)
sudo cp cadence-caddy-ca.crt /usr/local/share/ca-certificates/
sudo update-ca-certificates

# 2. reboot-required flag (Debian minimal does not create /var/run/reboot-required)
sudo apt install update-notifier-common

# 3. rebuild + reinstall the agent (adds cadence-agent-poll.{service,timer})
cd cadence && git pull
cd agent && CGO_ENABLED=0 go build -trimpath -o bin/cadence-agent ./cmd/agent
sudo systemd/install.sh

# 4. point the agent at the HTTPS entrypoint
sudo sed -i 's#^CADENCE_SERVER_URL=.*#CADENCE_SERVER_URL=https://cadence.lan#' \
  /etc/cadence/agent.env

# 5. verify
sudo systemctl start cadence-agent.service
journalctl -u cadence-agent -n 20 --no-pager     # "report sent to https://cadence.lan ..."
systemctl list-timers 'cadence-agent*'           # both timers listed, poll every 1 min
```

The dashboard should show the host with agent `0.3.0` and a green freshness
dot. Once every monitored VM uses `https://cadence.lan`, tighten the server:
set `CADENCE_BACKEND_BIND=127.0.0.1` (and `CADENCE_FRONTEND_BIND=127.0.0.1`) in
`.env` and `docker compose up -d`.

## Step 1 — run the database

Requires Docker with the `compose` plugin.

```sh
cp .env.example .env          # set POSTGRES_PASSWORD (first boot only -- see the note there)
docker compose up -d db
```

`up -d db` starts an empty PostgreSQL. **The schema is created by Alembic**, run
from the backend/scheduler entrypoint (`app.prestart`) the first time the full
stack comes up (Step 6) -- there is no `init.sql` bootstrap.

### Verify (after the full stack is up)

```sh
docker compose ps                                      # db healthy
docker compose run --rm backend alembic current        # 0006 (head)

# Tables: hosts, packages, host_packages, reports, jobs, schedules, scheduler_state, alembic_version
docker compose exec db psql -U cadence -d cadence -c '\dt'
docker compose exec db psql -U cadence -d cadence -c '\dx'   # pgcrypto extension
```

### Reset

To wipe the database and rebuild it from scratch:

```sh
docker compose down -v
docker compose up -d --build        # Alembic rebuilds the schema from revision 0001
```

## Database migrations (Alembic)

Alembic owns the schema. Revision `0001` is the full V1 baseline; every change
after it is a hand-written revision under `backend/alembic/versions/` (no
autogenerate). `backend/app/db/init.sql` is kept only as a reference copy of
`0001` for diffing an old database -- it is **not** applied anywhere.

**On a normal deploy there is nothing to run.** The backend/scheduler image
entrypoint (`backend/entrypoint.sh` -> `python -m app.prestart`) waits for the
database and runs `alembic upgrade head` before the app starts; the backend and
the scheduler serialise on a Postgres advisory lock so only one migrates. A
fresh database is built straight from `0001`. So a deploy is just:

```sh
git pull && docker compose up -d --build
docker compose run --rm backend alembic current        # sanity: shows the applied revision
```

**Adopting a pre-Alembic database** (one bootstrapped from the old `init.sql`
mount, so it has the baseline tables but no `alembic_version`): `app.prestart`
detects this and runs `alembic stamp 0001` automatically before upgrading. To do
it by hand instead:

```sh
docker compose run --rm backend alembic stamp 0001     # record the baseline, runs no DDL
docker compose up -d                                    # applies every later revision
```

Before stamping, confirm the database really matches the baseline: dump its
schema (`pg_dump --schema-only`) and diff it against `backend/app/db/init.sql`.
If it differs, reconcile before stamping.

## Backup & restore

Two things are irreplaceable on the central server: the Postgres database
(`pgdata`) and **Caddy's data volume** (`caddy_data`) — it holds the internal
CA. Lose the CA and every monitored VM fails TLS silently until it is
re-provisioned with a new root.

### Back up (run on the server, stack up)

```sh
scripts/backup.sh
# -> backups/<UTC timestamp>/{db.dump,caddy_data.tgz,env,MANIFEST}
```

`db.dump` is `pg_dump -Fc`; `caddy_data.tgz` is the whole Caddy `/data` volume;
`env` is a copy of `.env` (**secrets** — admin key, DB password — mode 0600);
`MANIFEST` records the git commit, Alembic revision and sha256 sums.
`CADENCE_BACKUP_DIR` sets the location, `CADENCE_BACKUP_KEEP` how many to retain
(default 14). Installed on the central server as a nightly user cron job
(`crontab -l`); `backup.sh` cd's into its own repo, so call it by absolute path:

```cron
MAILTO=""
15 3 * * *  /home/cadence/cadence/scripts/backup.sh >> /home/cadence/cadence/backups/backup.log 2>&1
```

### Verify a backup restores

`scripts/restore-check.sh [backup-dir]` restores the newest (or given) backup
into throwaway `cadence-rt-*` containers/volumes, checks it, and tears them
down — it never touches the live stack. It asserts: `pg_restore` loads the
dump, the Alembic revision matches the manifest, `GET /api/v1/hosts` on a
backend bound to the restored DB matches the live host list, and the restored
CA both verifies its issued cert and is trusted by a Caddy started on the
restored volume. Run it after any change to the backup script and periodically
against a real backup.

### Restore for real

On a fresh host (or after wiping the volumes), from a checkout at the commit in
`MANIFEST`:

```sh
B=backups/<timestamp>
cp "$B/env" .env                                   # or merge secrets by hand

# database
docker compose up -d db
until docker compose exec -T db pg_isready -U cadence -d cadence; do sleep 1; done
docker compose exec -T db pg_restore -U cadence -d cadence --clean --if-exists < "$B/db.dump"

# Caddy CA + certs
docker run --rm -v cadence_caddy_data:/v -v "$PWD/$B":/b:ro postgres:16 \
  sh -c 'tar xzf /b/caddy_data.tgz -C /v'

docker compose up -d
docker compose run --rm backend alembic current   # sanity: matches MANIFEST
```

The volume names are `<project>_pgdata` / `<project>_caddy_data` (`project` =
the repo directory name, `cadence`). If you restore onto a stack that already
has data, `docker compose down -v` first — that is destructive, take a backup
immediately before.

## Step 2 — run the backend

```sh
docker compose up -d --build db backend
curl -s http://127.0.0.1:8000/healthz          # {"status":"ok"}
```

Interactive API docs: <http://127.0.0.1:8000/docs>.

Config comes from environment variables only (`app/core/config.py`):
`POSTGRES_*` build the DB URL (or set `CADENCE_DATABASE_URL` directly),
`CADENCE_ADMIN_KEY` is the shared secret for the admin endpoints.

### Provision a host (admin)

```sh
curl -s -X POST http://127.0.0.1:8000/api/v1/admin/hosts \
  -H "X-Admin-Key: $CADENCE_ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"hostname":"vm-web-01","description":"web frontend"}'
# -> {"id":"...","hostname":"vm-web-01","token":"..."}   (token shown once)
```

Only the sha256 of the token is stored (`hosts.token_hash`).

### Send a report (agent token)

```sh
curl -s -X POST http://127.0.0.1:8000/api/v1/reports \
  -H "Authorization: Bearer <token>" \
  -H 'Content-Type: application/json' \
  -d @report.json
# -> {"host_id":"...","installed_package_count":N,
#     "updates_available_count":N,"security_updates_count":N,"reboot_required":bool}
```

`report.json` follows the agent payload in `CLAUDE.md` section 5. Each report
**replaces** the host's current package state (`host_packages`) and appends a
row to `reports` with the raw payload. `packages` is a shared dimension table —
rows are created on demand, never deleted.

### Read the dashboard views (no auth)

```sh
curl -s http://127.0.0.1:8000/api/v1/hosts            # list + computed status
curl -s http://127.0.0.1:8000/api/v1/hosts/<host_id>  # detail + package list
```

`status` is derived from the host's current `host_packages`:
`security_updates_available` > `updates_available` > `up_to_date`. The list is
ordered by hostname. `last_seen_at` is the report-freshness signal for the UI.
These GET endpoints are unauthenticated in V1 (single-user, trusted network).

### Run the backend tests

`pytest` against a real PostgreSQL (the code uses JSONB / `gen_random_uuid()` /
`FOR UPDATE SKIP LOCKED`). Test deps live in `backend/requirements-dev.txt` and
are **not** in the runtime image. With the compose stack up:

```sh
docker compose run --rm -v "$PWD/backend:/app" backend \
  sh -c "pip install -q -r requirements-dev.txt && pytest -q"
```

It drops `cadence_test`, rebuilds it with `alembic upgrade head` (so the
migrations are exercised), and rolls back each test in a transaction. Point it
elsewhere with `TEST_DATABASE_URL`.

## Step 4 — the agent

Go, stdlib only, single static binary, one-shot (collect → POST → exit).
Scheduling is external (systemd timer, step 5). Lives in `agent/`.

### Build

```sh
cd agent
CGO_ENABLED=0 go build -trimpath -o bin/cadence-agent ./cmd/agent
go test ./...
```

### Configuration (environment only)

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `CADENCE_SERVER_URL` | yes | — | Base URL of the backend, e.g. `https://cadence.lan` |
| `CADENCE_TOKEN` | yes | — | Per-host token from `POST /api/v1/admin/hosts` |
| `CADENCE_RUN_APT_UPDATE` | no | `false` (installer sets `true`) | Run `apt-get update` before collecting so the dashboard reflects current apt state; without it, freshness follows the system's `apt-daily` timer. Needs root; failure is non-fatal |
| `CADENCE_HTTP_TIMEOUT_SECONDS` | no | `30` | HTTP client timeout |

### Run once

```sh
CADENCE_SERVER_URL=http://127.0.0.1:8000 \
CADENCE_TOKEN=<token> \
./agent/bin/cadence-agent
# cadence-agent: report sent to ...: host=... packages=N updates=N security=N reboot_required=false
```

What it collects: installed packages via
`dpkg-query -W -f='${Package}\t${Architecture}\t${Version}\n'`; pending updates
via `apt-get -s dist-upgrade` (parsing `Inst` lines); OS info from
`/etc/os-release`; reboot flag from `/var/run/reboot-required`. The
security-vs-normal split is a **heuristic**: the substring `security` in a
package's apt origin string. No home-grown version comparison — apt decides what
is an update.

## Step 5 — schedule the agent (systemd timers)

Two oneshot units on their own timers (`agent/systemd/`):

| Unit | Cadence | Does |
|---|---|---|
| `cadence-agent.service` / `.timer` | `OnBootSec=1min`, then **every 30 min** (`RandomizedDelaySec=5min`) | full package/OS report, runs a piggybacked job if any |
| `cadence-agent-poll.service` / `.timer` | **every 1 min** (`-poll`) | claims a pending job and runs it — no collection; nothing pending → exits silently |

| File | Installed as |
|---|---|
| `agent.env.example` | `/etc/cadence/agent.env` (chmod 0600, holds the token) |
| `install.sh` | installer (binary + all four units, enables both timers) |

The 1-min poll is what makes a dashboard-triggered upgrade start within a
minute (see Step 8). Don't want it? `systemctl disable --now
cadence-agent-poll.timer` — jobs then wait for the 30-min report instead.

### Install on a monitored VM

```sh
# on the VM, from a checkout of this repo
cd agent
CGO_ENABLED=0 go build -trimpath -o bin/cadence-agent ./cmd/agent
sudo systemd/install.sh
sudo editor /etc/cadence/agent.env      # set CADENCE_SERVER_URL + CADENCE_TOKEN
```

The service has no `[Install]` section — it is triggered only by the timer (or a
manual `systemctl start`), never at boot directly.

### Verify

```sh
systemctl start cadence-agent.service                 # run once now
journalctl -u cadence-agent -n 20 --no-pager          # "report sent to ..."
systemctl list-timers 'cadence-agent*'                # both timers
```

## Step 6 — the dashboard

React + Vite + Tailwind, in `frontend/`. One master-detail view (no router),
polls the API every 30s. Served in production by nginx, which also proxies
`/api/` to the `backend` service (so the browser hits a single origin, no CORS).
nginx resolves `backend` per request via Docker DNS, so recreating the backend
container does not need a frontend restart.

Part of the compose stack — `docker compose up -d --build` builds and runs it.
Open it through Caddy at `https://cadence.lan/` (`CADENCE_SITE_ADDRESS`). For a
TLS-less local look, set `CADENCE_FRONTEND_BIND=0.0.0.0` and open
`http://<server-ip>:8080/`.

The list shows each host with a status badge (green `up to date` / amber
`updates` / red `security`), the update counts, a **last-seen freshness**
indicator (green ≤ 5 min, amber ≤ 15 min, red beyond — spots a stopped agent),
and a `reboot` marker. The detail pane shows host metadata and the package
table (pending updates first, security flagged), with a toggle to show all
installed packages.

The tight freshness thresholds assume `cadence-agent-poll.timer` is enabled (it
is by default), which refreshes `last_seen_at` every minute. A host running only
the periodic report will always read amber/red.

### Local development

```sh
cd frontend
npm install
npm run dev            # http://localhost:5173 , proxies /api to http://localhost:8000
```

Point the dev proxy elsewhere with `CADENCE_DEV_API=http://<host>:8000 npm run dev`.

## Step 8 — apply updates (jobs)

Trigger `apt-get dist-upgrade` on a host from the dashboard; the agent picks it
up within ~1 minute (the poll timer) and posts the log back.

```
dashboard --POST /admin/hosts/{id}/jobs (X-Admin-Key)--> job: pending
agent     --POST /agent/next-job (every 1 min)---------->  claims it, job: running
          (also delivered on the periodic POST /reports)
agent     runs `apt-get dist-upgrade -y` (non-interactive, confold/confdef)
agent     --POST /jobs/{id}/result--------------------->  job: succeeded | failed (+ log)
```

- **~1-minute latency**, from the `cadence-agent-poll.timer` (Step 5). No push /
  long-poll — the agent still only makes outbound calls. Impatient? still
  `systemctl start cadence-agent.service`.
- **One active job per host** (`409` otherwise). The poll and the report both
  use `SELECT … FOR UPDATE SKIP LOCKED`, so only one ever claims a given job.
- **Reboot** (see below). `reboot_required` is true when
  `/var/run/reboot-required` exists (created by **`update-notifier-common`** on
  Ubuntu / Debian ≤12, **`reboot-notifier`** on Debian 13) **or** an installed
  kernel image of the running flavour is newer than the running kernel (agent
  ≥0.6.0, no package needed). `scripts/agent-install.sh` still installs a helper
  when one is available.
- Agent kill-switch: `CADENCE_ENABLE_UPGRADES=false` in `agent.env` — a
  triggered job is then reported back as `failed` with that reason.
- The dashboard asks for the `X-Admin-Key` once (kept in `sessionStorage`) the
  first time you trigger a job.

### Reboot after an upgrade

Each host has a **`reboot_policy`**: `never` (default — the agent only reports
`reboot_required`) or `auto` (the agent reboots when the upgrade left a reboot
pending). Set it from the host detail pane, or
`PATCH /api/v1/admin/hosts/{id}` with `{"reboot_policy": "auto"}`.

A single job can override it: the trigger control sends `params.reboot`
(`auto` / `never`); an override wins over the host policy. The server resolves
the effective value when it hands the job to the agent and records it on the
job.

The agent reboots (`systemctl --no-block reboot`) only when **all** of: the
upgrade succeeded, `/var/run/reboot-required` is present, the effective mode is
`auto`, and `CADENCE_ENABLE_REBOOT` is not `false` (`agent.env` kill-switch,
default true). It always posts the job result first, so the job never hangs in
`running`.

### Scheduled maintenance windows

Each host can have **one** recurring window (`schedules` table, `UNIQUE`
per host). The `scheduler` service — the same image as the backend, run as
`python -m app.scheduler`, no ports — wakes every minute and turns a due window
into an `apt_upgrade` job (`requested_by = "scheduler"`). Agents still pull
jobs, so the outbound-only model is untouched.

- **`weekly`** (`weekday` 0–6, Monday = 0) or **`monthly`** (`day_of_month`
  1–28), at `hour`:`minute` in the schedule's `timezone`. Coherence, ranges and
  `params.reboot` are enforced by DB `CHECK`s, not only the API.
- `params.reboot` (`auto` / `never`) overrides the host `reboot_policy` for the
  jobs this window creates, exactly like a manual trigger.
- If the host already has an active job when the window opens, that run is
  **skipped** and the schedule advances to the next window (no catch-up).

Manage it from the host detail pane, or:

```sh
curl -s -X POST https://cadence.lan/api/v1/admin/hosts/<id>/schedules \
  -H "X-Admin-Key: $CADENCE_ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"kind":"weekly","weekday":6,"hour":4,"minute":0,"timezone":"Europe/Paris","params":{"reboot":"auto"}}'
# GET  /api/v1/hosts/<id>/schedules        (no auth, like the other reads)
# PATCH/DELETE /api/v1/admin/schedules/<schedule_id>   (X-Admin-Key)
```

### Retention

The `reports` and `jobs` tables are append-only. Once a day the scheduler
prunes them:

- `CADENCE_REPORTS_RETENTION_DAYS` (default 90) — deletes `reports` rows older
  than that (the raw report log).
- `CADENCE_JOBS_RETENTION_DAYS` (default 90) — deletes `succeeded` / `failed`
  jobs completed longer ago; `pending` / `running` are never touched.

Set either to `0` to keep forever. `host_packages` is replaced whole on every
report, so it does not grow. Look for `retention sweep: …` in
`docker compose logs scheduler`.

### Test the full flow on a monitored VM

The VM needs a current agent with the poll timer — see
"Upgrade an existing monitored VM" above. Then:

```sh
# from the dashboard: open the host, "trigger dist-upgrade", enter the admin key
journalctl -u cadence-agent-poll -f              # within ~1 min: job received -> apt -> result
```

The job row in the dashboard goes `pending → running → succeeded`, with the
apt output under "log".
