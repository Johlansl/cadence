# Cadence server — changelog

The server — backend, dashboard and the `docker compose` stack — ships as one
unit under a single version (`backend/app/__init__.py` `__version__`,
`frontend/package.json`). The agent is versioned separately; see
[`agent/CHANGELOG.md`](agent/CHANGELOG.md) and
[`docs/decisions.md`](docs/decisions.md) "Versioning".

## 0.2.0

- Security-advisory enrichment. The `scheduler` service refreshes Debian's
  `DSA/list` + `DLA/list` every 6 h into new `advisories` / `advisory_packages`
  tables (migration `0010`). `GET /api/v1/hosts/{id}` and `GET /api/v1/packages`
  now return an `advisories` list on each pending security update — DSA/DLA id,
  tracker URL and CVE ids — and the dashboard shows it as a link beside the
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

First public release (Apache-2.0). The stack — `db`, `backend`, `scheduler`,
`frontend`, `caddy` — is what has been running in production; this entry is a
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
