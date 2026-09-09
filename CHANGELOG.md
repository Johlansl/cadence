# Cadence server: changelog

The server, backend, dashboard and the `docker compose` stack, ships as one
unit under a single version (`backend/app/__init__.py` `__version__`,
`frontend/package.json`). The agent is versioned separately; see
[`agent/CHANGELOG.md`](agent/CHANGELOG.md) and
[`docs/decisions.md`](docs/decisions.md) "Versioning".

## Unreleased

- Package exclusions (holds). A new `package_exclusions` table (migration
  `0015`) lets an operator keep packages or families off automatic upgrades,
  globally or per host (`postgresql-14`, `docker-ce`, `linux-image*`,
  `nvidia*`, ...), matched with a glob. The server resolves rules to an exact
  package list per host and puts it in an `apt_upgrade` job's
  `params.excluded_packages`; the agent (>= 0.10.1) validates every name and
  reconciles dpkg's hold state toward it, diffed against
  `params.known_held_packages` (the Cadence-managed hold set the server last
  recorded, from the previous job's `result.held_packages`) rather than a
  live `apt-mark showhold` read, so a hold neither wanted nor previously
  recorded by Cadence -- an operator's, unattended-upgrades', a distro
  default -- is never touched. Reports `held_conflicts` when apt shows real
  evidence a hold blocked or degraded the run. `GET /hosts` and `GET
  /hosts/{id}` gain a recomputed `excluded_count` and a per-package
  `excluded` flag; a new `#exclusions` dashboard section manages the rules
  (create/delete only, no in-place edit); `job.succeeded` / `job.failed`
  webhook bodies gain `held_conflicts`. No tag-scoped rules yet (roadmap
  item 6).
- Failed jobs are classified. A failed job now carries a coarse
  `failure_category` (`apt_locked`, `network_or_repo`, `dpkg_error`,
  `disk_full`, `timeout`, `agent_lost`, `agent_refused`, `unknown`) and a
  one-line `failure_summary` (migration `0014`: two nullable columns on
  `jobs`, no CHECK, not backfilled). The agent (>= 0.9.0) classifies from the
  output of the apt/dpkg command that failed; the scheduler reaper classifies
  a job it fails with no agent result (`timeout` if the host is still
  reporting, else `agent_lost`) and now also emits a `job.failed` webhook for
  it. The two fields appear in the jobs API, as a pill plus summary line on
  the dashboard, and in the `job.failed` webhook body (with a `reaped` flag).
  An older agent leaves the category `null`.
- Outbound webhooks. A new `webhooks` section on the dashboard registers HTTP
  endpoints that Cadence POSTs a signed JSON body to when an event fires:
  `job.succeeded` / `job.failed` (on a job result), `host.reboot_required` (on
  the false to true edge), `host.security_updates_available` (on a changed
  non-zero security count), `host.offline` (a host quiet past
  `CADENCE_WEBHOOK_OFFLINE_AFTER_SECONDS`, detected on the scheduler tick).
  Delivery is an outbox: the event is persisted in the same transaction as the
  change (migration `0013`: `webhooks`, `webhook_deliveries`,
  `webhook_host_state`), then the `scheduler` service drains it with retry and
  exponential backoff (`CADENCE_WEBHOOK_MAX_ATTEMPTS`, up to 1 h between
  attempts) and an explicit per-attempt timeout
  (`CADENCE_WEBHOOK_TIMEOUT_SECONDS`). Each POST carries
  `X-Cadence-Signature` (HMAC-SHA256 over the send timestamp and the body
  hash, keyed with the webhook secret) and `X-Cadence-Timestamp` for replay
  rejection, the same construction as agent requests. Admin routes
  (`POST/PATCH/DELETE /api/v1/admin/webhooks`, `POST .../{id}/test`) are
  `X-Admin-Key`-guarded; the `GET` views are unauthenticated like the other
  dashboard reads but the stored URL is masked and the signing secret, like
  the full URL, is shown only once at creation. All new `CADENCE_WEBHOOK*`
  settings are optional and the feature is inert until a webhook is configured.
  `docs/webhooks.md` is the operator guide: the payload of each event, a
  signature-verification receiver to copy, and how to relay to Discord / Slack
  / Teams (which need their own message format, not this generic body).
- `GET /api/v1/packages` is now keyset-paginated, matching `/hosts`,
  `/hosts/{id}/jobs`, `/hosts/{id}/reports` and `/admin/audit`: `limit`
  (default 50, max 500) plus an `after` / `after_id` cursor holding the last
  row's package name and architecture. The default cap replaces what was an
  unbounded `(package x host)` scan on every dashboard poll. The response
  shape is unchanged (a plain list of package groups). The Packages view gains
  a "Load more" button when a full page is returned.
- **Breaking (agent auth).** The server now accepts only signed agent requests
  (`X-Cadence-Token-Hash` / `X-Cadence-Timestamp` / `X-Cadence-Signature`,
  agent `0.8.0`+); the legacy `Authorization: Bearer <token>` path in
  `get_current_host` is removed. A host running an agent older than `0.8.0`, or
  a `0.8.0`+ agent still on a token issued before the signed-request scheme
  shipped (no stored encrypted copy), loses access and must be rotated onto a
  fresh token and upgraded. A partial set of the signed headers is still
  rejected outright. No schema change.
- Bumped FastAPI (`0.115.6` → `0.141.1`) and Starlette (`0.41.3`, transitive →
  `1.6.0`, now pinned) to clear the security advisories accumulated on the old
  Starlette, including CVE-2026-48710 (unvalidated `Host` header used to rebuild
  `request.url`) and CVE-2025-62727 (quadratic `Range` header parse in
  `FileResponse`). No wire, behaviour or schema change; three deprecated
  `status.HTTP_413_REQUEST_ENTITY_TOO_LARGE` / `HTTP_422_UNPROCESSABLE_ENTITY`
  constants were renamed to their `*_CONTENT_TOO_LARGE` / `*_UNPROCESSABLE_CONTENT`
  equivalents (same status codes). `pytest` also moved `8.3.4` → `9.1.1`
  (dev-only, clears PYSEC-2026-1845).

## 0.2.0

- Security-advisory enrichment. The `scheduler` service refreshes Debian's
  `DSA/list` + `DLA/list` every 6 h into new `advisories` / `advisory_packages`
  tables (migration `0010`). `GET /api/v1/hosts/{id}` and `GET /api/v1/packages`
  now return an `advisories` list on each pending security update, DSA/DLA id,
  tracker URL and CVE ids, and the dashboard shows it as a link beside the
  `SEC` badge. Linkage is an exact match of an advisory's per-release fixed
  version against apt's candidate; the `security` heuristic and all fleet
  counts are unchanged. Disable the feed with
  `CADENCE_ADVISORY_REFRESH_ENABLED=false`.
- Advisory linkage now prefers the source package and release codename reported
  by agent `0.7.0` (migration `0011` adds `host_packages.source_package` and
  `hosts.os_codename`), so a library binary like `libssl3` links via its source
  (`openssl`) without relying on the curated fallback map, and a release outside
  the built-in `VERSION_ID` list still resolves. Pre-`0.7.0` agents keep working
  through the fallbacks.
- Release automation. A `v*` tag on GitHub builds and pushes
  `ghcr.io/johlansl/cadence-{backend,frontend}` and cuts a GitHub Release;
  `docker-compose.release.yml` is a new overlay that runs those images instead
  of building from source. Images and the `agent-v*` binaries carry a Sigstore
  build-provenance attestation. See `docs/decisions.md` "Release automation".

## 0.1.0

First public release (Apache-2.0). The stack, `db`, `backend`, `scheduler`,
`frontend`, `caddy`, is what has been running in production; this entry is a
baseline, not a diff.

Fleet inventory:

- Agents report installed packages and available apt updates. The dashboard
  shows per-host status, last-seen staleness, pending-update counts and a
  security-update count (substring heuristic on the apt origin, not CVE
  linkage).
- Append-only `reports` history per host.

Actions:

- Per-host `apt-get dist-upgrade` and reboot, run as queued jobs: at-least-once
  handoff to the agent, result callback, and scheduler reaping of jobs that
  outrun a running timeout.
- Per-host `reboot_policy` (`auto` / `never` / `prompt`), overridable per job;
  `reboot_required` surfaced on the host.

Scheduling:

- `scheduler` service with a persisted heartbeat. Per-host `weekly` / `monthly`
  maintenance windows (`schedules` table) that auto-queue upgrade jobs.

Authentication & hardening:

- Per-host bearer tokens for agents (`agent_tokens`), with rotate / revoke /
  expiry. Read and admin API gated by `CADENCE_ADMIN_KEY`.
- Caddy reverse proxy: TLS (internal CA by default), enforcing
  Content-Security-Policy and security headers, dashboard and read/admin API
  behind basic-auth, agent bootstrap assets served over plain HTTP.
- Non-blocking auth-failure throttle; `CADENCE_TRUSTED_PROXIES` for
  X-Forwarded-For trust.
- Admin actions recorded in an `audit` table; keyset-paginated `GET
  /admin/audit`.

Operations:

- Alembic migrations run on boot under a Postgres advisory lock; schema at
  revision `0008`.
- `/healthz` and `/readyz`; compose healthchecks, resource limits and log
  rotation on every service.
- Scripts: `backup`, `restore-check`, `provision-host`, `publish-agent`,
  `gen-secrets`, `agent-install`; nightly backup cron.
