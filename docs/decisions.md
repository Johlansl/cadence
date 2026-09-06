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
  Documented as a heuristic, not ground truth. This is what sets
  `host_packages.is_security_update` and drives every fleet security count.
- **Advisory linkage sits on top of that heuristic, it does not replace it.**
  The scheduler pulls Debian's `DSA/list` + `DLA/list` every 6 h into the
  `advisories` / `advisory_packages` tables, and `GET /hosts/{id}` /
  `GET /packages` link each apt-flagged pending security update to the DSA/DLA
  whose per-release `fixed_version` **equals** apt's candidate version (an
  exact string match — Cadence still does no version comparison of its own).
  It is deliberately *not* a full vulnerability scan: no unfixed / no-DSA CVEs,
  no severity. Agent `0.7.0`+ reports each binary's Debian source package
  (`dpkg-query ${source:Package}`) and the release codename
  (`/etc/os-release VERSION_CODENAME`), which the read-path uses directly; for
  older agents it falls back to a name-based binary→source mapping with a small
  curated table for common libraries and a `VERSION_ID`→codename table.
- **The agent never reboots on its own** unless the host's `reboot_policy` is
  `auto` (or a job overrides it) *and* the kill-switch `CADENCE_ENABLE_REBOOT`
  is not `false`. It otherwise just reports `reboot_required`.

## Authentication

- **Bearer tokens per host** (`agent_tokens` table), SHA-256-hashed at rest,
  transmitted once at issue time. One is created at provisioning; more can be
  issued so a token is rotated roll-forward (new one issued, agent moved onto
  it, old one revoked) with no reporting gap. Each token has an optional
  `expires_at` and a `revoked_at`; state is *derived* from those two, there is
  no separate flag. Revocation is auth-plane only — it never touches queued or
  running jobs (deactivate the host for that). Simple, and enough for a
  single-operator tool. Remaining limitation: tokens default to no expiry.
- **A single shared `X-Admin-Key`** guards every admin write. Changing a system
  warrants more than an anonymous GET. It can be rotated live via
  `CADENCE_ADMIN_KEY_PREVIOUS`. Known limitation: total blast radius — a leaked
  key can queue an upgrade or reboot on the whole fleet. Successful writes are
  appended to `audit_log` in the same transaction as the mutation, but the
  shared key means the recorded actor is only what the caller put in an
  optional `X-Actor` header (default `admin`), not a proven identity.
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

## Versioning

Two version lines on purpose:

- **The server** — backend, frontend and the `docker compose` stack — ships as
  one unit under a single version (`backend/app/__init__.py` `__version__`,
  `frontend/package.json`; `0.1.0` at first public release). They are always
  deployed together, so one number is enough.
- **The agent** carries its own (`agent/CHANGELOG.md`; `0.6.x`, `0.7.x`). It is
  distributed and upgraded separately, runs against a range of server versions,
  and had a release history before the repo went public — forcing it back to
  `0.1.0` would erase that. It reports its version on every report so the
  dashboard shows what each host runs.

A release build stamps the agent version from the newest `agent-v*` git tag
reachable from `HEAD` (`git describe`, `agent-v` prefix stripped), injected at
link time by `scripts/publish-agent.sh` via
`-ldflags "-X main.agentVersion=<version>"`. `agent/CHANGELOG.md` headings track
that tag (`## 0.7.0` ↔ `agent-v0.7.0`). The `agentVersion` literal in
`agent/cmd/agent/main.go` is only the fallback for dev / untagged builds; the
tag is the source of truth for anything published. The server version stays a
hand-set string (`backend/app/__init__.py`).

The two version lines do not need to match; the server's API stays backward
compatible within a minor line.

## Agent distribution / signing

The agent binary, its SHA-256, the systemd units and the internal CA are staged
into `dist/` by `scripts/publish-agent.sh` and served by Caddy over **plain
HTTP** at `/install.sh` and `/agent/*`, so a host can fetch them before it
trusts the CA (`SECURITY.md`, "Agent bootstrap is trust-on-first-use").

- **Releases are minisign-signed.** `scripts/publish-agent.sh` signs the binary
  when a private key is present, and — once `agent/minisign.pub` is committed —
  *refuses to publish unsigned* rather than silently dropping the signature.
  The signing key is passwordless, kept at `~/.cadence/minisign.key` (outside
  the repo, gitignored). CI builds a SHA-256-only artifact on purpose: no
  signing key is exposed to CI.
- **The signature only helps out of band.** `agent/minisign.pub` is committed
  for convenience, but the installer fetches everything over the same
  unauthenticated HTTP channel, so verification adds tamper-resistance *only*
  when the operator passes the key to the installer as `CADENCE_MINISIGN_PUB`
  from a copy obtained separately (the repo, a password manager, …). This is
  the documented path for anything past a trusted LAN; on the LAN target the
  SHA-256 (a truncation guard) is what actually runs by default.
- **The CA is still trust-on-first-use.** minisign covers the *binary* only;
  the CA certificate is fetched and trusted over plain HTTP with no
  fingerprint check. Unchanged, and out of scope here.
- **Forking Cadence.** A third party who redeploys this repo inherits the
  upstream `agent/minisign.pub` and cannot hold its private half, so their
  `scripts/publish-agent.sh` fails the "unsigned release" guard by
  construction. They must replace `agent/minisign.pub` with their own key
  (`minisign -G -W -p agent/minisign.pub -s ~/.cadence/minisign.key`) or delete
  it to publish unsigned. Called out in `publish-agent.sh` at the guard.
- **Key backup.** `scripts/backup-signing-key.sh` writes a passphrase-protected
  copy of the key to `~/.cadence/minisign.key.enc` (`minisign -C`, scrypt) and
  `scripts/backup.sh` folds that already-encrypted copy into every nightly
  backup dir. The live key stays passwordless (`publish-agent.sh` needs it); a
  plaintext copy is never written anywhere. The passphrase is typed into
  `minisign`'s own prompt and kept by the operator (password manager) — never
  on the box, in the repo, or in a command. It is a *stronger* bar than
  `backups/<ts>/env` on purpose: `env`'s secrets only attack this one server,
  the signing key forges releases for the whole fleet from anywhere.
- **Key restore.** `scripts/restore-signing-key.sh` takes a backup (or a
  `minisign.key.enc`), prompts for the passphrase, installs the passwordless
  key, and refuses to install it unless a fresh signature verifies against the
  committed `agent/minisign.pub`. `restore-check.sh` check 5 asserts the backup
  is present and encrypted.
- **Key rotation** (written down so it is not improvised; not yet executed):
  1. `minisign -G -W -p /tmp/new.pub -s ~/.cadence/minisign.key.new`.
  2. Replace `~/.cadence/minisign.key` with the new secret key; copy the new
     public key over `agent/minisign.pub`.
  3. Commit + push `agent/minisign.pub` to both remotes.
  4. `scripts/backup-signing-key.sh --force` with a fresh passphrase.
  5. `scripts/deploy.sh` — re-signs `dist/agent/` with the new key.
  6. Re-roll every host with the new `CADENCE_MINISIGN_PUB`.
  There is no transition window to manage: a host keeps running its installed
  agent until step 6 re-rolls it, and is never "stuck" because the operator
  re-runs the installer on each. A zero-touch multi-key rotation (installer
  accepting several keys) is only needed if hosts self-update without the
  operator — they do not — so it stays with the deferred release-automation
  work.

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
