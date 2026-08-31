# Architecture

Cadence is a lightweight patch-management tool for Linux servers: an agent
reports installed packages and available updates, a backend stores fleet state
and queues actions, and a dashboard shows status and triggers upgrades or
reboots per host.

## Components

The central server runs as a `docker compose` stack:

| Service | Image | Role |
|---|---|---|
| `db` | `postgres:16` | fleet state, reports, jobs, schedules |
| `backend` | built from `backend/` | FastAPI HTTP API (agent ingest + dashboard reads + admin writes) |
| `scheduler` | same image as `backend`, `python -m app.scheduler` | turns due `schedules` into `jobs`, reaps stuck jobs, runs the daily retention sweep, records a heartbeat |
| `frontend` | built from `frontend/` (`nginx:1.27-alpine` serving a Vite build) | the dashboard; nginx also proxies `/api/` to `backend` |
| `caddy` | `caddy:2-alpine` | the single public entrypoint, TLS terminated with an internal CA; also serves the plain-HTTP agent bootstrap assets |

Each monitored host runs the **agent**: a single static Go binary (stdlib only),
invoked one-shot by two `systemd` timers — a ~30 min full report and a ~1 min
job poll.

## Communication model

**Outbound-only.** The agent always initiates; the server never connects to a
host. There is no long-poll and no daemon:

- `POST /api/v1/reports` — the agent sends installed packages + available apt
  updates + OS info + reboot-required state. The response body may carry one
  pending job for that host (*piggyback*), so a triggered upgrade can start on
  the next report without any push channel.
- `POST /api/v1/agent/next-job` — a dedicated fast poll (~1 min) that claims a
  pending job without re-collecting package state, so a dashboard-triggered
  action starts within about a minute instead of waiting for the next report.

Job claiming uses `SELECT ... FOR UPDATE SKIP LOCKED`, so the report path and
the poll path racing on the same job is safe — exactly one claims it.

## Authentication

- **Agent → server:** one bearer token per host, generated server-side at host
  creation and transmitted once. Only its SHA-256 hash is stored.
- **Admin writes** (create/delete hosts, queue jobs, edit schedules): a single
  shared `X-Admin-Key` header. The dashboard keeps it in `sessionStorage` and
  prompts for it on the first write of a session.
- **Dashboard reads** (`GET /api/v1/hosts`, `/hosts/{id}`, `/hosts/{id}/jobs`,
  `/fleet/summary`, …): **unauthenticated**. V1 is single-user and assumes the
  API sits on a trusted network behind the reverse proxy. See
  [decisions.md](decisions.md) and [../SECURITY.md](../SECURITY.md).

## Data model

PostgreSQL, schema owned by Alembic (`backend/alembic/versions/`; revision
`0001` is the full baseline). Core tables:

- `hosts` — one row per monitored host: identity, OS, `package_manager`,
  `tags` (jsonb), `reboot_policy` (`auto` / `never` / `prompt`), `is_active`,
  `last_seen_at`.
- `packages` — a shared `(name, architecture)` dimension, never deleted.
- `host_packages` — the current per-host package state (installed version,
  candidate version, security flag). Replaced wholesale on every report.
- `reports` — an append-only log of each report (counters + the raw payload).
- `jobs` — queued/running/finished actions (`apt_upgrade`, `reboot`), with a
  jsonb `params` and a captured `log`.
- `schedules` — one maintenance window per host (`weekly` / `monthly`).
- `scheduler_state` — small key/value store (last retention sweep, heartbeat).

Extensibility is built in without over-engineering: `os_family` /
`package_manager` leave room for non-apt package managers, `jobs.params` and
`schedules.params` are jsonb, `hosts.tags` is groundwork for grouping.

## Deployment notes

- **Migrations run on boot.** The backend/scheduler entrypoint waits for the
  database and runs `alembic upgrade head` before starting; the two containers
  serialise on a Postgres advisory lock. A fresh database is built straight
  from revision `0001`. There is no `init.sql` bootstrap.
- **TLS.** Caddy issues certificates from its own internal CA. The CA root must
  be trusted on every monitored host and dashboard client (the agent verifies
  against the system trust store). The `caddy_data` volume holds the CA — back
  it up. A public-domain / ACME setup requires editing the `Caddyfile`.
- **Agent bootstrap.** `scripts/publish-agent.sh` stages the binary, checksum,
  units and CA into `dist/`, which Caddy serves over **plain HTTP** at
  `/install.sh` and `/agent/*` so a host can fetch them before it trusts the
  CA. This is a trust-on-first-use step — see [../SECURITY.md](../SECURITY.md).
- **Health & limits.** Every service has a healthcheck; startup is ordered
  `db → backend → frontend → caddy`. Per-service memory/CPU limits and
  json-file log rotation are set for a small (2 vCPU / 2 GB) host.
