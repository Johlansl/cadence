# Cadence

Lightweight patch management for Linux servers. See `CLAUDE.md` for the full
V1 brief, scope and architecture decisions.

## Status

Incremental build, one testable step at a time (`CLAUDE.md` section 8).

- [x] **Step 1 — database scaffolding**: `docker-compose.yml` (db service only)
      + `init.sql` schema applied on first startup.
- [x] **Step 2 — backend (FastAPI)**: `GET /healthz`,
      `POST /api/v1/admin/hosts`, `POST /api/v1/reports`.
- [x] **Step 3 — backend read views**: `GET /api/v1/hosts`,
      `GET /api/v1/hosts/{id}`.
- [x] **Step 4 — Go agent**: collect (dpkg / apt / os-release) + report.
- [x] **Step 5 — systemd unit + timer** for the agent.
- [x] **Step 6 — frontend**: master-detail dashboard (React + Vite + Tailwind).
- [ ] Step 7 — end-to-end validation on real VMs.

## Real-VM test setup

Target topology: one **central server VM** running `db` + `backend` + `frontend`
via docker-compose, and one or more **monitored Debian VMs** each running the
agent on a systemd timer. The agent only talks outbound to the server.

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
| `CADENCE_BACKEND_BIND` | `0.0.0.0` (so monitored VMs can reach the API) |
| `CADENCE_FRONTEND_BIND` | `0.0.0.0` (so you can open the dashboard) |

```sh
docker compose up -d --build
curl -s http://<server-ip>:8000/healthz          # {"status":"ok"}
# dashboard: http://<server-ip>:8080/
```

`restart: unless-stopped` brings the services back after a reboot. Restrict
ports 8000 / 8080 to your VM subnet at the firewall. TLS is **not** included
yet — a Caddy reverse proxy in front is the intended next step; ask if you want
it now.

### 2. Register each monitored VM (run on the server, or anywhere with the admin key)

```sh
curl -s -X POST http://<server-ip>:8000/api/v1/admin/hosts \
  -H "X-Admin-Key: $CADENCE_ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"hostname":"vm-web-01","description":"web frontend"}'
# -> {"id":"...","hostname":"vm-web-01","token":"..."}   keep the token
```

### 3. On each monitored VM

Build the (static) binary — on the VM if Go is available, or once elsewhere and
`scp` `bin/cadence-agent` + the `agent/systemd/` directory over:

```sh
cd agent
CGO_ENABLED=0 go build -trimpath -o bin/cadence-agent ./cmd/agent
sudo systemd/install.sh
sudo editor /etc/cadence/agent.env      # CADENCE_SERVER_URL=http://<server-ip>:8000 , CADENCE_TOKEN=<token>
```

Verify:

```sh
sudo systemctl start cadence-agent.service
journalctl -u cadence-agent -n 20 --no-pager         # "report sent to ..."
systemctl list-timers cadence-agent.timer            # next scheduled run
curl -s http://<server-ip>:8000/api/v1/hosts         # the VM shows up, last_seen_at fresh
```

## Step 1 — run the database

Requires Docker with the `compose` plugin.

```sh
cp .env.example .env          # adjust POSTGRES_PASSWORD
docker compose up -d db
```

### Verify

```sh
# Container healthy, init.sql executed on first startup
docker compose ps
docker compose logs db

# Tables: hosts, packages, host_packages, reports, jobs
docker compose exec db psql -U cadence -d cadence -c '\dt'

# Named indexes
docker compose exec db psql -U cadence -d cadence -c '\di'

# pgcrypto extension
docker compose exec db psql -U cadence -d cadence -c '\dx'
```

### Reset

The schema is applied only on an empty data volume. To replay `init.sql`:

```sh
docker compose down -v
docker compose up -d db
```

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
| `CADENCE_RUN_APT_UPDATE` | no | `false` | Run `apt-get update` before collecting (needs root; failure is non-fatal) |
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

## Step 5 — schedule the agent (systemd timer)

Files in `agent/systemd/`:

| File | Installed as |
|---|---|
| `cadence-agent.service` | `/etc/systemd/system/` — `Type=oneshot`, reads `/etc/cadence/agent.env` |
| `cadence-agent.timer` | `/etc/systemd/system/` — `OnBootSec=5min`, then hourly, `RandomizedDelaySec=5min`, `Persistent=true` |
| `agent.env.example` | `/etc/cadence/agent.env` (chmod 0600, holds the token) |
| `install.sh` | one-shot installer (copies binary + units, enables the timer) |

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
systemctl list-timers cadence-agent.timer             # next scheduled run
```

## Step 6 — the dashboard

React + Vite + Tailwind, in `frontend/`. One master-detail view (no router),
polls the API every 30s. Served in production by nginx, which also proxies
`/api/` to the `backend` service (so the browser hits a single origin, no CORS).

Part of the compose stack — `docker compose up -d --build` builds and runs it.
With `CADENCE_FRONTEND_BIND=0.0.0.0`, open `http://<server-ip>:8080/`.

The list shows each host with a status badge (green `up to date` / amber
`updates` / red `security`), the update counts, a **last-report freshness**
indicator (green ≤ 90 min, amber ≤ 6 h, red beyond — spots a stopped agent),
and a `reboot` marker. The detail pane shows host metadata and the package
table (pending updates first, security flagged), with a toggle to show all
installed packages.

### Local development

```sh
cd frontend
npm install
npm run dev            # http://localhost:5173 , proxies /api to http://localhost:8000
```

Point the dev proxy elsewhere with `CADENCE_DEV_API=http://<host>:8000 npm run dev`.
