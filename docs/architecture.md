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
  only while not expired/revoked and its host is `is_active`. The agent
  (`0.8.0`+) sends a signed request (HMAC over the request, keyed with the
  token) instead of the token itself; `token_hash` is the lookup key and the
  Fernet-encrypted plaintext is what verifies the HMAC (see `SECURITY.md`). A
  token from before this scheme has no encrypted copy and cannot authenticate
  until its host is rotated onto a fresh one.
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
- `agent_tokens`: per-host tokens (SHA-256 hash as the lookup key, a
  Fernet-encrypted copy for HMAC verification, optional `expires_at` /
  `revoked_at`, `last_used_at`). Several may be active for roll-forward
  rotation.
- `packages`: a shared `(name, architecture)` dimension, never deleted.
- `host_packages`: the current per-host package state (installed version,
  candidate version, security flag, and the Debian `source_package` when the
  agent reports it). Replaced wholesale on every report.
- `reports`: an append-only log of each report (counters + the raw payload).
- `jobs`: queued/running/finished actions (`apt_upgrade`, `reboot`,
  `apt_dry_run`), with a jsonb `params` and a captured `log`. `apt_dry_run`
  is a pure-read simulation: the agent runs `apt-get -s dist-upgrade` and
  reports, in `result.dry_run`, what a real `apt_upgrade` would do (packages
  it would upgrade / newly install / remove, the ones apt keeps back, the
  ones a Cadence exclusion rule filters out, and the ones already on hold on
  the box). It never runs `apt-mark`, `dpkg`, or a real upgrade, and it runs
  even where `CADENCE_ENABLE_UPGRADES=false`. A failed job also carries a
  coarse
  `failure_category` and a one-line `failure_summary`: the agent classifies
  from the output of the apt/dpkg command that failed (or reports `timeout` /
  `agent_refused`), and the scheduler reaper classifies a job it failed with
  no agent result (`timeout` if the host is still reporting, else
  `agent_lost`). The set is open (no CHECK); `unknown` is the honest default.
  An `apt_upgrade` job's `params.excluded_packages` is the exact list of
  package names the server resolved from `package_exclusions` for that host;
  `params.known_held_packages` is the Cadence-managed hold set the server
  last recorded (the previous `apt_upgrade` job's `result.held_packages`).
  A successful or failed result's `result.held_conflicts` names any held
  package apt showed real evidence of skipping or blocking on that run;
  `result.held_packages` is what Cadence actually holds after reconciliation,
  fed back as the next job's `known_held_packages`. An `apt_dry_run` job also
  gets `params.excluded_packages` (same resolution as `apt_upgrade`), but not
  `params.known_held_packages`: it never reconciles, it only filters its own
  preview.
- `package_exclusions`: operator hold rules (`scope` `global` or `host`,
  `host_id` nullable, `pattern` a glob matched with Python's `fnmatch`).
  Additive across scopes, no re-inclusion, no tag scope yet. Resolved to
  exact package names against a host's known inventory at job-creation time;
  the pattern itself never reaches the agent. The agent reconciles dpkg's
  real hold state (`apt-mark hold`/`unhold`) toward `excluded_packages`,
  diffed against `known_held_packages` -- **not** a live `apt-mark showhold`
  read -- so a hold neither wanted nor previously recorded by Cadence (an
  operator's, unattended-upgrades', a distro default) is never touched. A
  rule is created or deleted, not edited in place.
- `schedules`: one maintenance window per host (`weekly` / `monthly`).
- `advisories` / `advisory_packages`: Debian DSA/DLA advisories (id, CVE ids,
  URL) and the per-release source-package fixed versions the read API joins
  pending security updates against. Refreshed by the scheduler.
- `scheduler_state`: small key/value store (last retention sweep, last
  advisory refresh, heartbeat).
- `audit_log`: append-only trail of successful admin writes, one row per
  mutating `X-Admin-Key` call, written in the mutation's own transaction.
- `webhooks` / `webhook_deliveries` / `webhook_host_state`: outbound
  notification config, the delivery outbox, and per-host "already notified"
  bookkeeping. See "Webhooks" below.

Extensibility is built in without over-engineering: `os_family` /
`package_manager` leave room for non-apt package managers, `jobs.params` and
`schedules.params` are jsonb, `hosts.tags` is groundwork for grouping.

## Webhooks

Cadence emits outbound notifications for five events: `job.succeeded` /
`job.failed` (when an agent submits a job result, or the scheduler reaps a job
that never reported back), `host.reboot_required` (a
report flips the host from not-needing to needing a reboot),
`host.security_updates_available` (a report's security-update count is non-zero
and differs from the last one notified for that host), and `host.offline` (a
host's `last_seen_at` is older than `CADENCE_WEBHOOK_OFFLINE_AFTER_SECONDS`,
checked on the scheduler tick; it fires once and re-arms when the host reports
again).

**Outbox, not inline.** The request or scheduler code that observes an event
inserts a `webhook_deliveries` row (one per subscribed enabled webhook) in the
*same database transaction* as the change that produced it, then returns. No
HTTP happens on that path, so a slow or dead endpoint never blocks a report,
a job callback, or the scheduler, and a delivery is never lost to a crash.

**Dispatch.** Every scheduler pass (~60 s) `dispatch_pending_deliveries` claims
a batch of due `pending` rows (`FOR UPDATE SKIP LOCKED`), POSTs each body, and
on failure reschedules with exponential backoff (60 s, 120 s, ... capped at
1 h) up to `CADENCE_WEBHOOK_MAX_ATTEMPTS`, after which the row is left
`failed`. Each attempt has an explicit `CADENCE_WEBHOOK_TIMEOUT_SECONDS`
timeout. First-attempt latency is therefore up to one tick. Terminal rows are
pruned by the daily retention sweep
(`CADENCE_WEBHOOK_DELIVERIES_RETENTION_DAYS`).

**Body and signature.** The body is
`{event_type, timestamp, delivery_id, data}` (`timestamp` is when the event
occurred; `data` is event-specific). Each POST carries `X-Cadence-Event`,
`X-Cadence-Delivery`, `X-Cadence-Timestamp` (this attempt's send time, fresh
per retry) and `X-Cadence-Signature` = `HMAC-SHA256(secret,
"{X-Cadence-Timestamp}\n{sha256_hex(body)}")`. This is the agent
request-signing construction (see "Authentication") reduced to the two parts a
receiver can reproduce; the timestamp is inside the signed scope, so a replay
with a new timestamp fails the HMAC and a verbatim replay fails the receiver's
freshness check.

**Config.** `POST/PATCH/DELETE /api/v1/admin/webhooks` and
`POST /api/v1/admin/webhooks/{id}/test` need `X-Admin-Key`; the `GET` views are
unauthenticated like the other dashboard reads, but the URL is masked and the
signing secret, like the full URL, is only ever in the creation response. The
test endpoint writes a real `webhook.test` outbox row delivered by the same
dispatcher.

The operator-facing guide, [docs/webhooks.md](webhooks.md), has the per-event
payload catalogue, a copy-pasteable signature-verification receiver, and how to
relay to Discord / Slack / Teams (which need their own message shape, not this
generic body).

## Deployment notes

- **Redeploy.** `scripts/deploy.sh` on the server is the one command: it
  rebuilds and restarts the stack, prints the migration head, re-stages the
  agent bootstrap assets, and recreates Caddy so a host-edited `Caddyfile` or
  the newly staged assets take effect (Caddy bind-mounts those paths by inode,
  so a plain restart keeps serving the old content). `docker compose up -d
  --build` alone works for a stack-only change but does neither the agent
  restage nor the Caddy recreate.
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
