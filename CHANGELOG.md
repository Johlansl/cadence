# Cadence server: changelog

The server, backend, dashboard and the `docker compose` stack, ships as one
unit under a single version (`backend/app/__init__.py` `__version__`,
`frontend/package.json`). The agent is versioned separately; see
[`agent/CHANGELOG.md`](agent/CHANGELOG.md) and
[`docs/decisions.md`](docs/decisions.md) "Versioning".

## Unreleased

Sequenced reboots (6A), fleet self-upgrade of the agent (6B), and agent releases
signed in CI with a two-key trust set. The agent side is in
[`agent/CHANGELOG.md`](agent/CHANGELOG.md) 0.15.0.

- Sequenced, budgeted multi-host reboots (6A). Agents now report the kernel boot
  id and whether a job will reboot the host. The backend records the observed
  boot (migrations `0024` `hosts.current_boot_id` and `0025`
  `campaign_hosts.awaited_boot`) and a campaign no longer completes a host that
  is rebooting until a different boot plus fresh acceptable health proves it
  came back. An expected reboot with no proof, or a wait that stays unproven past
  `CADENCE_CAMPAIGN_RETURN_TIMEOUT_SECONDS` (default 1800), halts the campaign
  fail-safe. The new `CADENCE_MAX_CONCURRENT_REBOOTS` (default 0, which keeps the
  previous behaviour: no limit) bounds unproven reboots fleet-wide under a
  transaction advisory lock. An unproven reboot hold is never released
  implicitly: retention spares those jobs, and only a new boot or an explicit
  operator recovery clears it. See `docs/campaigns.md`.
- Agent fleet upgrades (6B). A new closed job type, `agent_upgrade` (migration
  `0026`), takes exactly `{"target_version": "N.N.N"}` and nothing else, and
  campaigns can use it (migration `0027`: canary stage, observation gate,
  blast-radius halt). The server never completes it on the word of the process
  that replaces the binary: success needs a later authenticated contact from the
  host showing the target version, and `CADENCE_UPGRADE_PROOF_TIMEOUT_SECONDS`
  (default 600, a non-zero value below 420 is refused at startup) fails upgrades
  that never prove. Hosts need agent 0.15.0 at the canonical path
  `/usr/bin/cadence-agent` first: see `docs/agent-path-migration.md` and
  `scripts/check-agent-host-upgrade-readiness.sh`. Production rollout:
  `docs/agent-upgrade-campaigns-runbook.md`. `scripts/test-agent-upgrade-e2e.sh`
  is the real end-to-end proof.
- Agent releases are signed in CI, with a two-key trust set. An `agent-v*` tag
  runs the `build`, `sign` and `publish` jobs of `release.yml`; `sign` runs in
  the GitHub environment `release` with the secret `MINISIGN_SECRET_KEY`, and
  `publish` creates the Release as a draft that the maintainer verifies and
  publishes. The trusted keys are `agent/minisign.pub` (active, `C9EDE89E1DEEED37`)
  and the new `agent/minisign-backup.pub` (offline, `2167C9939EC0F4FC`), both
  compiled into the agent; a signature from either is accepted.
  `scripts/publish-agent.sh fetch <version>` is now the normal way to stage a
  release on the server (download, verify against both keys, stage atomically;
  `CADENCE_FETCH_REPO` points it at a fork); `publish <version> <dir>` stays for
  an air-gapped server or a break-glass release. The server needs `minisign` and
  `curl` and must be linux/amd64. `scripts/agent-install.sh` takes an optional
  second trusted key, `CADENCE_MINISIGN_PUB2`; both values are shape-checked
  before anything is downloaded and the log names the key that verified. The
  `README.md` example that passed the whole content of `agent/minisign.pub` as
  `CADENCE_MINISIGN_PUB` did not work (minisign's `-P` takes a bare key, not a
  two-line file) and is corrected; the installer now refuses that form
  explicitly. See `README.md`,
  `SECURITY.md` ("Release signing", "If a signing key is compromised") and
  `docs/decisions.md` ("Release automation").
- Changes for whoever runs the signing tools by hand (these are breaking for
  scripts that call them). `scripts/build-agent-release.sh` is now the
  break-glass path: `CADENCE_SIGNING_KEY` is required (the `~/.cadence/minisign.key`
  default is gone), `CADENCE_RELEASE_PUBKEY` is removed (the script refuses to run
  if it is set), and the signatures are verified against the whole trust set.
  `scripts/restore-signing-key.sh` needs `--to <destination>` and an explicit
  source, restores the key still encrypted (it no longer writes a passwordless
  key), and refuses any destination inside a git repository.
  `scripts/backup-signing-key.sh` takes the key file as an argument and checks
  that it is one of the two trusted keys. The agent upgrade E2E now always runs
  in a throwaway copy of the checkout with its own disposable `.env`, names its
  Docker image `e2e-backend` instead of `cadence-backend`, and refuses to start
  with less than 4 GiB free for Docker.
- Breaking change for `scripts/agent-install.sh` with `CADENCE_UPGRADE_ONLY=true`:
  it now refuses to install a release older than the one the host runs, and
  therefore requires `minisign` and `CADENCE_MINISIGN_PUB` on the host (it exits
  before downloading anything otherwise). The running version is the highest
  `-version` of `/usr/bin/cadence-agent` and the historical
  `/usr/local/bin/cadence-agent`; no agent at either path, a symlink or a path
  that does not report a version also refuse. An equal version is accepted. The
  downloaded binary is executed (`-version`, empty environment, 30 second
  timeout) only after its SHA-256 and signature are verified. There is no
  override. See `SECURITY.md` "Rollback".
- Audit of the shell scripts. `scripts/backup.sh` now prunes only the directories
  named like the backups it creates (`YYYYMMDDTHHMMSSZ`), never a symlink, and
  counts only those toward `CADENCE_BACKUP_KEEP`: a `CADENCE_BACKUP_DIR` that also
  holds other directories no longer loses them. `scripts/rotate-dashboard-password.sh`
  removes its temporary copy of `.env` (secrets included) when it fails.
  `agent-install.sh`, `agent-bootstrap.sh` and `build-agent-release.sh` now stop
  on SIGHUP, SIGINT or SIGTERM (exit 129, 130, 143) instead of going on after
  deleting their working directory.
- `scripts/agent-install.sh` now looks for `minisign` before it downloads anything
  when `CADENCE_MINISIGN_PUB` is set, and reads the served `cadence-agent.sha256`
  as strictly as the agent and `publish-agent.sh` do (one line, 64 lowercase hex
  characters, two spaces, and the name `cadence-agent` or
  `cadence-agent-linux-amd64`). A server that stages the triplet with
  `publish-agent.sh` already serves a conforming file. `publish-agent.sh` also
  refuses a version older than the newest one it holds, and takes the key ID of
  the two trusted keys from the key bytes rather than from the `.pub` comment.

## 0.5.0

- RBAC v1 for OIDC accounts: reader by default, operator for the emails
  in the new `CADENCE_OIDC_OPERATOR_EMAILS` setting (resolved at login,
  sealed in the session; pre-RBAC sessions read as reader). All writes
  need the operator role, the four reads that required the key need any
  authenticated caller, public reads are unchanged, and the shared
  `X-Admin-Key` stays a total bypass. Breaking change for existing OIDC
  deployments: an unlisted account becomes a reader at next login
  (list your own account before deploying; boot warns when OIDC is on
  with an empty allowlist). Covered by `test_rbac.py` (role resolution,
  named-permission dependencies, exact 28-route permission mapping plus
  a filet against unguarded admin routes, live reader/operator matrix)
  and dashboard role gating in `SessionAuth` (`useAdminKeyAction`
  refuses readers locally; the backend stays the boundary).
  See `docs/oidc.md` "Roles" and `SECURITY.md`.

## 0.4.1

- Job robustness, no new capability. One active job per host is now
  enforced in PostgreSQL by a partial unique index
  (`ux_jobs_one_active_per_host`, migration `0023`), replacing the
  application-level SELECT-then-INSERT check that raced under
  concurrency; a concurrent `23505` on that index maps back to host-busy
  in `create_job_for_host` via a savepoint, so every caller keeps its
  contract. Job completion (`submit_job_result`) now runs as a single
  conditional `UPDATE ... WHERE status='running' ... RETURNING` instead
  of read-then-write, so exactly one concurrent or late submission wins
  and every other gets 409 without writing the job, the host projection
  or the outbox. Covered by new concurrency tests on real PostgreSQL
  transactions (`test_jobs_active_guard.py`, `test_robustness_t0.py`).
- Dashboard polish: shared "attention" filter between the sidebar and
  Fleet Overview, hidden-by-filters selection count in the bulk action
  bar, dynamic confirmation dialog and contrast fixes.

## 0.4.0

- Admin SSO via OIDC (Authentik as reference). Dashboard operators can sign
  in at the provider; the backend validates the ID token locally and seals
  a stateless session cookie (8 h, HttpOnly/Secure/Lax, no DB table, no new
  dependency). Writes in a session audit under the verified subject and
  ignore `X-Actor`; the shared `X-Admin-Key` keeps working exactly as
  before, so a dead provider never locks out the admin. Inert unless
  `CADENCE_OIDC_ENABLED=true` with issuer, client id/secret and redirect
  URI all set. The issuer tolerates one trailing slash on either side;
  provider transport failures answer 502/401, never 500. See `docs/oidc.md`.
- CVE severity (CVSS). Every CVE already linked via a DSA/DLA can now show a
  cached CVSS base score and severity, resolved per CVE from the NVD CVE API
  2.0 by the scheduler (new `cve_scores` table, migration `0022`, refreshed
  on the advisory tick) and served by `GET /api/v1/hosts/{id}` and
  `GET /api/v1/packages` as a per-advisory rollup (highest known score).
  The DSA/DLA matching is unchanged, a CVE with no known score shows nothing
  extra (unknown, never zero), and the mapping stays best-effort: the absence
  of a score or an advisory never means a host is unaffected. Disable the
  lookup with `CADENCE_CVE_SCORE_REFRESH_ENABLED=false`; an optional
  `CADENCE_NVD_API_KEY` raises the NVD rate allowance. No agent change.

## 0.3.0

- Agent enrollment and mTLS transport authentication. A short-lived,
  single-use enrollment code (`cad1.<192-bit-secret>.<server-CA-SHA256>`)
  pins the server's CA before any executable artifact is fetched, closing
  the first-contact gap where a MITM serving the install script over HTTP
  could otherwise substitute it. `POST /api/v1/agent/enroll` atomically
  consumes one code and issues both an HMAC token and a 90-day ECDSA P-256
  client certificate from a new private client PKI (migration `0020`:
  `enrollment_codes`, `agent_certificates`). Agent traffic moves to a
  dedicated `:8443` listener requiring `client_auth mode
  require_and_verify`; the backend additionally binds the verified
  certificate's host to the same host authenticated by the existing
  per-request HMAC signature, so a certificate and a token issued to
  different hosts never validate together. `CADENCE_LEGACY_AGENT_ENDPOINTS`
  gated the transitional dual-mode window during fleet migration; both
  hosts (`vm-japp`, `vm-nginxproxy`) are migrated and it is now `off`.
  Certificates renew automatically 14 days before expiry
  (`POST /api/v1/agent/certificate/renew`); the previous certificate stays
  valid until the switch completes, so a lost renewal response cannot lock
  an agent out. A new `#enrollment` dashboard view administers codes and
  certificates. `scripts/backup.sh` / `restore-check.sh` now cover the
  client PKI. Migration `0021` fixes an oversight in `0020`: deleting a
  host could leave an `enrollment_codes` row either silently vanishing
  (audit loss, `target_host_id` was `ON DELETE CASCADE`) or blocking the
  delete outright (`enrollment_codes_consumption_check` rejecting the row
  `ON DELETE SET NULL` left behind); both host references on
  `enrollment_codes` now go `ON DELETE SET NULL` and the row always
  survives as an orphaned audit record. See `docs/enrollment-mtls.md`.
- Agent `0.13.0`: a standalone `health_check` job type. It reruns an
  `apt_upgrade`'s post-check phase (dpkg audit, apt dependencies, disk
  space, failed services, reboot required) with no upgrade attached,
  refreshing `hosts.health_status` outside of an `apt_upgrade` result:
  manually from the dashboard (same pattern as `apt_dry_run`), and
  automatically once per boot via a new systemd unit pair that asks a new
  agent-token-authenticated `POST /api/v1/agent/health-check-job` to
  create-and-claim the job in one round trip. Migration `0019` widens
  `jobs.job_type`'s CHECK. A `health_check` result carries
  `post_checks`/`health_status` without `pre_checks`; the cross-field
  consistency rule that used to require `pre_checks` whenever
  `post_checks`/`health_status` are sent now lives in `submit_job_result`
  (job-type aware) instead of the shared Pydantic validator. Campaigns are
  unaffected: `campaigns.job_type` stays fixed to `apt_upgrade`.
- Agent `0.12.1`: `RestrictSUIDSGID=true` dropped from both `systemd` units. It
  was silently breaking upgrades of packages that ship setuid/setgid files
  (`shadow` -> `newgrp` / `chage`, `sudo`, `mount`, ...). Reliability of the
  upgrade path over a narrow layer of defence in depth; rationale and residual
  risk in [`SECURITY.md`](SECURITY.md) "The agent runs as root". Roll the fleet
  to `0.12.1` before relying on setuid-shipping upgrades succeeding.
- Pre- and post-upgrade health checks (roadmap item 7). Agent `0.12.0`+
  validates disk space, package-manager locks, dpkg and apt state, strict
  package-index refresh and a failed-service baseline before each
  `apt_upgrade`; a failed or unavailable blocking check prevents apt from
  running. After every attempted upgrade it checks dpkg, apt and disk again,
  diffs failed services and records the reboot signal. Ordered, bounded
  evidence is stored in `jobs.result.pre_checks` / `post_checks`; action status
  stays separate from derived host health (`healthy`, `degraded`, `unhealthy`,
  `unknown`). Migration `0018` adds the current `hosts.health_status` /
  `health_checked_at` projection without historical backfill. The dashboard
  shows both outcomes and expandable evidence; existing job webhooks gain the
  three health fields without new event types. Campaigns now advance after a
  successful action only for `healthy` / `degraded`, and stop on unhealthy or
  missing health, so campaign targets must be upgraded to agent `0.12.0` first.
  New server settings pin per-job thresholds:
  `CADENCE_UPGRADE_MINIMUM_AVAILABLE_BYTES` (1 GiB),
  `CADENCE_UPGRADE_BOOT_MINIMUM_AVAILABLE_BYTES` (200 MiB), and
  `CADENCE_UPGRADE_LOCK_WAIT_SECONDS` (120, max 3600). Older agents remain
  accepted for manual and scheduled jobs; their results leave host health
  unchanged. Also fixes the agent claiming a second job during the immediate
  post-job inventory report.
- Tag-scoped package exclusions. A hold rule can now be scoped to a tag
  (`scope='tag'`, new nullable `package_exclusions.tag`, migration `0017`):
  it applies to every host carrying that tag, where `tag` is a `"key"` /
  `"key=value"` query in the same shape as `GET /hosts?tag=` and campaign
  targeting. The three scopes (`global`, `host`, `tag`) are additive with no
  priority, resolved to exact package names server-side at job-creation like
  before, so the agent and its `params.excluded_packages` are unchanged.
  `GET /package-exclusions?host_id=` now also returns the tag rules that
  apply to that host. Related: `PATCH /api/v1/admin/hosts/{id}` lowercases
  tag keys and values on write (a pair that collides once lowercased is a
  422), so storage matches the already case-insensitive tag search; existing
  `hosts.tags` rows are not rewritten. No agent change (agent stays at
  0.11.0).
- Campaigns. A staged, rate-limited rollout of `apt_upgrade` jobs across a
  fixed host set: pick the hosts (an explicit list or a `tag` filter, resolved
  once at creation), split them into ordered waves (`stages`, e.g.
  `[2, "25%", "rest"]`), set a global `max_concurrency`, a `max_failures`
  budget and an observation window between waves. New tables `campaigns` /
  `campaign_hosts` and a nullable `jobs.campaign_id` (migration `0016`, which
  also finally adds the `jobs.job_type` CHECK, closed on the three existing
  values -- campaigns add no job type). A campaign is created in `draft`,
  started with a separate `POST /api/v1/admin/campaigns/{id}/activate`, and
  can be paused / resumed / cancelled; `GET /api/v1/campaigns` and
  `/campaigns/{id}` are dashboard reads. The engine is `advance_campaigns()`
  on the scheduler tick: it reconciles finished jobs (a failed job's
  `failure_category` maps to `skip` or `halt` via a code table), fills the
  active wave up to `max_concurrency` through the same job-creation path as
  the admin route and the scheduler, holds each terminal wave for its
  observation window before advancing, and stops the campaign on a `halt`
  disposition or once the skip count passes `max_failures` (any still-running
  host is then marked `orphaned`, its job left to finish on its own). Three
  webhook events: `campaign.stage_completed`, `campaign.completed`,
  `campaign.stopped`. New setting `CADENCE_CAMPAIGN_OBSERVATION_WINDOW_SECONDS`
  (default 600). No agent change (agent stays at 0.11.0). See
  [`docs/campaigns.md`](docs/campaigns.md).
- Dry-run. A new `apt_dry_run` job type previews what an `apt_upgrade` would
  do on a host without changing anything: the agent (>= 0.11.0) runs
  `apt-get -s dist-upgrade` and reports, in `result.dry_run`, the packages it
  would upgrade / newly install / remove, the ones apt keeps back, the ones a
  Cadence exclusion rule filters out, and the ones already on hold on the
  box. It never runs `apt-mark`, `dpkg`, or a real upgrade, and it runs even
  where `CADENCE_ENABLE_UPGRADES=false`. The job gets `params.excluded_packages`
  resolved the same way as an `apt_upgrade` (but no `known_held_packages`: it
  never reconciles). A "dry run" button next to "trigger dist-upgrade" on the
  host page queues one and renders the preview as a count strip plus a
  collapsible list per category. No migration (`jobs.job_type` stays free
  `TEXT`). `job.succeeded` / `job.failed` webhooks fire for a dry-run too;
  filter on `job_type` to skip them.
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
