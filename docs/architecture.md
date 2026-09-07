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
| `scheduler` | same image as `backend`, `python -m app.scheduler` | turns due `schedules` into `jobs`, reaps stuck jobs, runs the daily retention sweep, refreshes the Debian security-advisory feed, records a heartbeat |
| `frontend` | built from `frontend/` (`nginx:1.27-alpine` serving a Vite build) | the dashboard; nginx also proxies `/api/` to `backend` |
| `caddy` | `caddy:2-alpine` | the single public entrypoint, TLS terminated with an internal CA; also serves the plain-HTTP agent bootstrap assets |

Each monitored host runs the **agent**: a single static Go binary (stdlib only),
invoked one-shot by two `systemd` timers, a ~30 min full report and a ~1 min
job poll.

## Communication model

**Outbound-only.** The agent always initiates; the server never connects to a
host. There is no long-poll and no daemon:

- `POST /api/v1/reports`: the agent sends installed packages + available apt
  updates + OS info + reboot-required state. Each package also carries its
  Debian source package (`source_package`) and the report its release codename
  (`os_codename`), both since agent `0.7.0` and both optional, used to link
  security advisories precisely. The response body may carry one pending job
  for that host (*piggyback*), so a triggered upgrade can start on the next
  report without any push channel.
- `POST /api/v1/agent/next-job`: a dedicated fast poll (~1 min) that claims a
  pending job without re-collecting package state, so a dashboard-triggered
  action starts within about a minute instead of waiting for the next report.

Job claiming uses `SELECT ... FOR UPDATE SKIP LOCKED`, so the report path and
the poll path racing on the same job is safe, exactly one claims it.

## Authentication

- **Agent → server:** per-host tokens (`agent_tokens` table), generated
  server-side and transmitted once. Several can be active at once for
  roll-forward rotation; each has an optional `expires_at` (defaults to
  `CADENCE_TOKEN_DEFAULT_EXPIRY_DAYS`) / `revoked_at`. A token is accepted
  only while not expired/revoked and its host is `is_active`. Agent `0.8.0`+
  sends a signed request (HMAC over the request, keyed with the token) instead
  of the token itself; older agents still send it as a bearer value. Both
  forms hash to `token_hash`; the signed form also needs the plaintext,
  Fernet-encrypted at rest for that reason (see `SECURITY.md`).
- **Admin writes** (create/delete hosts, queue jobs, edit schedules): a single
  shared `X-Admin-Key` header. The dashboard keeps it in `sessionStorage` and
  prompts for it on the first write of a session. Each successful write appends
  a row to `audit_log`; `GET /api/v1/admin/audit` reads it back (same key).
- **Dashboard + read/admin API**: gated by a single shared HTTP basic-auth
  credential at Caddy (`CADENCE_DASHBOARD_*`, on by default; the agent endpoints
  above are exempt). No multi-user auth, no RBAC. See [decisions.md](decisions.md)
  and [../SECURITY.md](../SECURITY.md).

## Data model

PostgreSQL, schema owned by Alembic (`backend/alembic/versions/`; revision
`0001` is the full baseline). Core tables:

- `hosts`: one row per monitored host: identity, OS, `package_manager`,
  `tags` (jsonb), `reboot_policy` (`auto` / `never` / `prompt`), `is_active`,
  `last_seen_at`.
- `agent_tokens`: per-host tokens (SHA-256 hash for the bearer form, an
  encrypted copy for the signed form, optional `expires_at` / `revoked_at`,
  `last_used_at`). Several may be active for roll-forward rotation.
- `packages`: a shared `(name, architecture)` dimension, never deleted.
- `host_packages`: the current per-host package state (installed version,
  candidate version, security flag, and the Debian `source_package` when the
  agent reports it). Replaced wholesale on every report.
- `reports`: an append-only log of each report (counters + the raw payload).
- `jobs`: queued/running/finished actions (`apt_upgrade`, `reboot`), with a
  jsonb `params` and a captured `log`.
- `schedules`: one maintenance window per host (`weekly` / `monthly`).
- `advisories` / `advisory_packages`: Debian DSA/DLA advisories (id, CVE ids,
  URL) and the per-release source-package fixed versions the read API joins
  pending security updates against. Refreshed by the scheduler.
- `scheduler_state`: small key/value store (last retention sweep, last
  advisory refresh, heartbeat).
- `audit_log`: append-only trail of successful admin writes, one row per
  mutating `X-Admin-Key` call, written in the mutation's own transaction.

Extensibility is built in without over-engineering: `os_family` /
`package_manager` leave room for non-apt package managers, `jobs.params` and
`schedules.params` are jsonb, `hosts.tags` is groundwork for grouping.

## Deployment notes

- **Redeploy.** `scripts/deploy.sh` on the server is the one command: it
  rebuilds and restarts the stack, prints the migration head, and re-stages the
  agent bootstrap assets. `docker compose up -d --build` alone works for a
  stack-only change but does not restage the agent.
- **Migrations run on boot.** The backend/scheduler entrypoint waits for the
  database and runs `alembic upgrade head` before starting; the two containers
  serialise on a Postgres advisory lock. A fresh database is built straight
  from revision `0001`. There is no `init.sql` bootstrap.
- **TLS.** Caddy issues certificates from its own internal CA. The CA root must
  be trusted on every monitored host and dashboard client (the agent verifies
  against the system trust store). The `caddy_data` volume holds the CA, back
  it up. A public-domain / ACME setup requires editing the `Caddyfile`.
- **Agent bootstrap.** `scripts/publish-agent.sh` stages the binary, checksum,
  units and CA into `dist/`, which Caddy serves over **plain HTTP** at
  `/install.sh` and `/agent/*` so a host can fetch them before it trusts the
  CA. This is a trust-on-first-use step, see [../SECURITY.md](../SECURITY.md).
- **Health & limits.** Every service has a healthcheck; startup is ordered
  `db → backend → frontend → caddy`. Per-service memory/CPU limits and
  json-file log rotation are set for a small (2 vCPU / 2 GB) host.
