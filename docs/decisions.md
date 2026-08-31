# Design decisions

Why Cadence is built the way it is. See [architecture.md](architecture.md) for
the shape of the system and [../SECURITY.md](../SECURITY.md) for the threat
model.

## Communication

- **Outbound-only, no daemon.** The agent is a one-shot binary run by `systemd`
  timers; it collects, sends one report, and exits. Scheduling is `systemd`'s
  job, which is simpler and more observable (`journalctl`) than a hand-rolled
  loop, and it means nothing listens on the monitored hosts.
- **Piggyback + a dedicated job poll**, not long-poll / push / a short report
  timer. The report response can carry a pending job, and a separate ~1 min
  poll claims jobs without re-collecting package state. This keeps the
  outbound-only, one-shot model while giving triggered actions ~1 min latency,
  without re-simulating apt every minute or polluting the report log.

## Updates

- **`apt-get dist-upgrade`, not `upgrade`, for a job.** It applies everything
  the dashboard shows (kernels included) and is consistent with detection,
  which already uses `apt-get -s dist-upgrade`.
- **No home-grown version comparison.** Cadence trusts apt to say "an update is
  available"; it never compares version strings itself.
- **Security vs. normal is a heuristic**: the substring `security` in a
  package's apt origin string (e.g. `Debian-Security:13/stable-security`).
  Documented as a heuristic, not ground truth. There is no CVE/advisory linkage
  yet.
- **The agent never reboots on its own** unless the host's `reboot_policy` is
  `auto` (or a job overrides it) *and* the kill-switch `CADENCE_ENABLE_REBOOT`
  is not `false`. It otherwise just reports `reboot_required`.

## Authentication

- **One bearer token per host**, SHA-256-hashed at rest, transmitted once at
  provisioning. Simple, and enough for a single-operator tool. Known
  limitations: no rotation, no expiry, no revocation short of deactivating or
  deleting the host.
- **A single shared `X-Admin-Key`** guards every admin write. Changing a system
  warrants more than an anonymous GET. Known limitation: total blast radius and
  no audit trail — a leaked key can queue an upgrade or reboot on the whole
  fleet.
- **One shared basic-auth credential**, not multi-user auth. V1 has no RBAC, so
  Caddy gates the dashboard and the read/admin API with a single
  username/password (agent endpoints, which carry per-host tokens, are exempt).
  On by default; Caddy binds to loopback by default. Enough to keep a fleet
  inventory off the open internet without building a user system.

## Data & schema

- **`packages` is a shared dimension table**, never garbage-collected;
  `host_packages` is replaced wholesale on each report. `host_packages` holds
  only current state — history lives in the append-only `reports` payloads.
- **The agent's self-reported hostname wins** over the name entered at host
  creation: the agent is authoritative about its own identity.
- **Alembic owns the schema.** `init.sql` was the V1 first-boot bootstrap; it
  is now frozen as a reference copy of revision `0001` and is not applied
  anywhere. Every change is a hand-written revision (no autogenerate).

## Stack

- **Backend: ordinary dependencies** (FastAPI, SQLAlchemy sync, psycopg2,
  Pydantic, Alembic). **Agent: standard library only** — no config framework,
  no YAML, config via environment variables, one static `CGO_ENABLED=0`
  binary. The point is to stay light and trivially cross-compilable.
- **Frontend: React + Vite + Tailwind, no router.** One master/detail view,
  refreshed by ~30 s polling, no websocket. Dark theme only.
- **Pinned base images** (`postgres:16`, `node:22-alpine`, `nginx:1.27-alpine`,
  Go 1.23): stable, no reason to move.

## V1 scope

Deliberately **out of the initial version** (the data model stays extensible
for them, but there is no code):

- package exclusion / hold lists
- multi-host reboot sequencing / rollout batching
- notifications (Slack / email / webhooks)
- multi-distribution (dnf / RPM)
- multi-user authentication / RBAC

**Since added:** automatic scheduling / maintenance windows (the `schedules`
table + the `scheduler` service).
