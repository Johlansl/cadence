# Design decisions

Why Cadence is built the way it is. See [architecture.md](architecture.md) for
the shape of the system and [../SECURITY.md](../SECURITY.md) for the threat
model.

## Open-source posture

Cadence is an **actively open-source project now**, not a codebase that might be
opened later. It is meant for third parties to deploy on their own
infrastructure, and that audience is assumed *today*, so technical decisions
(security model, agent distribution, packaging, CI, release artefacts) are made
for an unknown external deployer, not only for this one installation.
Concretely: the trust model must survive an untrusted network, the agent must be
installable without a checkout of this repo, and release engineering (signed
multi-arch binaries, container images, `.deb` / `.rpm`) is real near-term work
rather than something to defer until someone asks.

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
  exact string match, Cadence still does no version comparison of its own).
  It is deliberately *not* a full vulnerability scan: no unfixed / no-DSA CVEs,
  no severity. Agent `0.7.0`+ reports each binary's Debian source package
  (`dpkg-query ${source:Package}`) and the release codename
  (`/etc/os-release VERSION_CODENAME`), which the read-path uses directly; for
  older agents it falls back to a name-based binary→source mapping with a small
  curated table for common libraries and a `VERSION_ID`→codename table.
- **The agent never reboots on its own** unless the host's `reboot_policy` is
  `auto` (or a job overrides it) *and* the kill-switch `CADENCE_ENABLE_REBOOT`
  is not `false`. It otherwise just reports `reboot_required`.
- **Dry-run is a separate job type (`apt_dry_run`), not a flag on
  `apt_upgrade`.** Mixing a system-changing action and a pure read in one type
  invites a wiring bug (a "dry-run" that actually upgrades, or the reverse); a
  distinct type makes the difference visible everywhere `job_type` already
  shows up (dashboard, webhooks, logs). The agent runs `apt-get -s
  dist-upgrade` and nothing that mutates the host: no `apt-mark`, no `dpkg`,
  no `-y` upgrade. It runs even when `CADENCE_ENABLE_UPGRADES=false`, because
  previewing pending changes on an upgrade-disabled host is useful and safe.
- **The dry-run preview reflects exclusion policy without applying it.** The
  server injects `params.excluded_packages` exactly as for `apt_upgrade`, and
  the agent drops matching names from the "would upgrade / would install"
  lists into a separate `excluded` list, even for a rule no real run has
  turned into an `apt-mark hold` yet. Separately it reports `held_in_place`:
  what apt itself kept back because of a hold already on the box (correlated
  the same way `held_conflicts` is on a real upgrade). So the operator sees
  both what is blocked now and what the next real run will block.
- **`jobs.job_type` has a DB CHECK** (`apt_upgrade`, `reboot`, `apt_dry_run`),
  added by migration `0016` alongside the campaigns tables. It was deliberately
  left free `TEXT` from `0001` until then, on the assumption campaigns would
  add a job type; they did not (a campaign orchestrates ordinary `apt_upgrade`
  jobs), so the CHECK simply closed on the three existing values. Widening it
  later for a genuinely new type is a drop + recreate, like `jobs.status`.
- **Health checks belong to `apt_upgrade`, not a new job type.** They protect
  every real upgrade regardless of whether it came from the dashboard, a
  schedule or a campaign. A separate check job would still leave a race before
  apt starts and would let some creation paths bypass the guard.
- **Action status and host health are separate.** `jobs.status` says whether
  the requested apt action completed. `result.health_status` says what the
  post-checks established about the machine. Thus `succeeded` plus
  `unhealthy` is valid and visible; changing the action to failed would hide
  the distinction this feature exists to provide.
- **Pre-check failures block; warnings do not.** Unavailable disk, lock, dpkg,
  apt or service-baseline evidence is not safe enough to start. Existing
  failed services and the compatibility fallback for old apt strict mode are
  warnings, preserving service-baseline comparison without making unrelated
  pre-existing damage an upgrade blocker.
- **Post-checks observe but do not repair.** The agent does not run fix-broken,
  restart units or clean filesystems. Newly failed services make health
  `unhealthy`; pre-existing failures and a pending reboot make it `degraded`.
  This keeps remediation explicit and avoids an upgrade job silently changing
  more than apt requested.
- **The host stores a projection, the job stores evidence.** Only
  `health_status` and `health_checked_at` are columns used by fleet and
  campaign decisions. Ordered check detail remains in `jobs.result`, avoiding
  a table per probe while keeping the exact run auditable. Existing hosts start
  `unknown`; historical results are not guessed.
- **Campaigns fail closed on missing health.** A successful job advances only
  with `healthy` or `degraded`. `unhealthy` stops with `health_unhealthy`, and
  missing, unknown or unrecognized health stops with `health_unknown`. This
  means campaign targets must run agent `0.12.0`+ after the server upgrade;
  manual and scheduled jobs remain compatible with older agents.
- **Rollout order for this change: the whole fleet moves to agent `0.12.0`
  before any campaign is activated.** After the server is upgraded, a campaign
  job that lands on a host still on `0.11.0` comes back `succeeded` with no
  `health_status`, which the fail-closed gate above reads as `health_unknown`
  and stops the campaign at its first host. The deploy itself needs no fleet
  change (manual and scheduled jobs keep working against older agents); it is
  only a precondition for campaigns. Repeated in the README "Campaigns"
  section so an operator sees it at activation time.
- **Not fixed here: the dedicated `reboot` job reports `succeeded` before
  `systemctl` runs.** The agent submits the job result and only then issues
  the reboot, and the server clears `host.reboot_required` on that result, so
  a reboot that fails to start leaves the flag briefly wrong. Severity is low:
  the agent exits non-zero and the next report re-sets `reboot_required` from
  `/run/reboot-required`, so the state self-corrects within one cycle. It is
  on the dedicated-reboot path, not the `apt_upgrade` health path this change
  is about, so it is deliberately deferred rather than fixed mid-change.

## Authentication

- **Tokens per host** (`agent_tokens` table), transmitted once at issue time.
  One is created at provisioning; more can be issued so a token is rotated
  roll-forward (new one issued, agent moved onto it, old one revoked) with no
  reporting gap. Each token has an optional `expires_at` and a `revoked_at`;
  state is *derived* from those two, there is no separate flag. Revocation is
  auth-plane only, it never touches queued or running jobs (deactivate the
  host for that). Simple, and enough for a single-operator tool. `expires_at`
  defaults to `CADENCE_TOKEN_DEFAULT_EXPIRY_DAYS` (365) when the caller does
  not pass one -- closes what used to be listed here as a remaining
  limitation ("tokens default to no expiry"); there is no supported way left
  to ask for one that never expires short of passing a far-future explicit
  date.
- **Signed requests, phase 1 of hardening agent<->server auth.** The token
  itself used to be sent as `Authorization: Bearer <token>` on every request.
  The agent (`0.8.0`+) instead sends `X-Cadence-Token-Hash` (the SHA-256
  already stored as `token_hash`, a non-secret lookup value the agent derives
  itself), `X-Cadence-Timestamp`, and `X-Cadence-Signature` (an HMAC-SHA256
  over `timestamp\nMETHOD\npath\nsha256(body)`, keyed with the real token,
  compared with `hmac.compare_digest`). The raw token no longer crosses the
  wire per call, which mainly matters for exposure through anything that logs
  or captures headers along the way, not the TLS-protected transport itself.
  This is the only accepted form (the bearer path was removed once the fleet
  had migrated, see "Transition" below); a partial set of the signed headers
  is rejected outright with its own error, so stripping one can never quietly
  change how the request is read.
  - **Verifying an HMAC needs the real secret, not a hash**, so
    `agent_tokens.secret_encrypted` holds a Fernet-encrypted copy
    (`CADENCE_TOKEN_ENCRYPTION_KEY` / `_PREVIOUS`, same rotation shape as
    `CADENCE_ADMIN_KEY`). This changes what a database compromise exposes: a
    stolen DB used to reveal nothing usable (a one-way hash only); now a row
    issued under this scheme also yields the plaintext to anyone who also
    holds the encryption key. Accepted for the same reason
    `CADENCE_ADMIN_KEY` already sits in `.env` as a plaintext credential:
    there is no alternative to holding the real secret if the server is
    ever going to verify a signature with it, and a compromised server was
    already fleet-fatal before this (it hands out root-level jobs to every
    host with no client-side confirmation). This is a same-server secret,
    not the minisign key's class of risk (that one forges releases
    fleet-wide independent of ever touching this server).
  - **A token issued before this shipped has no encrypted copy** and cannot
    authenticate at all -- its plaintext was never stored anywhere to begin
    with, so there is nothing to retrofit, and there is no other path left.
    Rotating the host onto a fresh token (the existing roll-forward flow
    above) is the only fix.
  - **Timestamp window: `CADENCE_SIGNATURE_WINDOW_SECONDS`, 300.** Not sized
    against how often the agent talks (~1 min job poll, ~30 min report) --
    that bounds request frequency, not signature freshness, and 300s sits
    well clear of both either way. Sized instead against clock skew
    (monitored hosts run NTP/systemd-timesyncd; skew is normally seconds,
    but a stalled sync should not fail every request the moment it drifts
    past a minute) and against bounding the one residual risk this scheme
    does not remove: a request captured inside its own window (not a leaked
    secret -- TLS already protects transit; this is about a compromised
    intermediate hop or a log capture) stays replayable, verbatim, once,
    until it expires. 300s matches Stripe's default webhook tolerance and
    sits under AWS SigV4's 15-minute upper bound. No nonce/replay tracking
    this phase -- real added infrastructure, overlapping the separately
    scoped rate-limiting work more than this auth phase.
  - **Transition, agent `0.7.0` (bearer-only) to `0.8.0`+ (signed), how it
    was done, no flag day:** the server shipped dual-mode first (bearer and
    signed both accepted). Then, per host, in this order (reversing it breaks
    the host until fixed): issue a fresh token (`POST .../tokens`, it gets
    both `token_hash` and `secret_encrypted`); roll the new secret into
    `/etc/cadence/agent.env` while the `0.7.0` binary is still running (it
    sends it as a bearer value, still accepted); revoke the old token;
    upgrade the binary to `>=0.8.0` (same `CADENCE_TOKEN`, it starts
    signing); confirm the host still reports. While both forms were live each
    request's scheme (`bearer`/`signed`) rode the structured request log
    (`request.state.auth_scheme`), so the migration could be confirmed
    complete fleet-wide from the logs, not a self-reported `agent_version`.
    Once no `bearer` hit had been seen across a full report+poll cycle on
    every active host, a dedicated commit deleted the bearer branch from
    `get_current_host`. Not a runtime flag: a real code change, the same
    shape as the pre-`0.7.0` advisories fallback above.
  - No server-side version gating anywhere in this: a request is accepted or
    rejected purely on the headers it carries and the token they name, never
    by parsing or comparing `hosts.agent_version` -- keeps the "no home-grown
    version comparison" rule intact.
- **A single shared `X-Admin-Key`** guards every admin write. Changing a system
  warrants more than an anonymous GET. It can be rotated live via
  `CADENCE_ADMIN_KEY_PREVIOUS`. Known limitation: total blast radius, a leaked
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
  only current state, history lives in the append-only `reports` payloads.
- **The agent's self-reported hostname wins** over the name entered at host
  creation: the agent is authoritative about its own identity.
- **Alembic owns the schema.** `init.sql` was the V1 first-boot bootstrap; it
  is now frozen as a reference copy of revision `0001` and is not applied
  anywhere. Every change is a hand-written revision (no autogenerate).

## Stack

- **Backend: ordinary dependencies** (FastAPI, SQLAlchemy sync, psycopg2,
  Pydantic, Alembic). **Agent: standard library only**, no config framework,
  no YAML, config via environment variables, one static `CGO_ENABLED=0`
  binary. The point is to stay light and trivially cross-compilable.
- **Frontend: React + Vite + Tailwind, no router.** One master/detail view,
  refreshed by ~30 s polling, no websocket. Dark theme only.
- **Pinned base images** (`postgres:16`, `node:22-alpine`, `nginx:1.27-alpine`,
  Go 1.23): stable, no reason to move.

## Versioning

Two version lines on purpose:

- **The server**: backend, frontend and the `docker compose` stack, ships as
  one unit under a single version (`backend/app/__init__.py` `__version__`,
  `frontend/package.json`; `0.1.0` at first public release). They are always
  deployed together, so one number is enough.
- **The agent** carries its own (`agent/CHANGELOG.md`; `0.6.x`, `0.7.x`). It is
  distributed and upgraded separately, runs against a range of server versions,
  and had a release history before the repo went public, forcing it back to
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

A published `agent-v*` tag is **never moved.** Between agent releases,
`git describe` reports `0.7.0-<n>-g<sha>` for a build made `n` commits past the
tag, that is accurate (the published binary is not at the tagged commit) and
the tag anchors the versioning *mechanism*, not the dashboard string. If the
long form is unwanted for a real re-roll, cut a fresh `agent-v0.7.x` tag (a new
anchor, even with no code change) rather than rewriting a tag that is already on
both remotes.

The two version lines do not need to match; the server's API stays backward
compatible within a minor line. They also have **separate release triggers**
(see "Release automation"): an `agent-v*` tag builds the agent, a `v*` tag
builds the server images. There is no combined tag, coupling them would force a
server release on every agent bump and vice versa.

## Agent distribution / signing

The agent binary, its SHA-256, the systemd units and the internal CA are staged
into `dist/` by `scripts/publish-agent.sh` and served by Caddy over **plain
HTTP** at `/install.sh` and `/agent/*`, so a host can fetch them before it
trusts the CA (`SECURITY.md`, "Agent bootstrap is trust-on-first-use").

- **Releases are minisign-signed.** `scripts/publish-agent.sh` signs the binary
  when a private key is present, and, once `agent/minisign.pub` is committed,
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
  `minisign`'s own prompt and kept by the operator (password manager), never
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
  5. `scripts/deploy.sh`, re-signs `dist/agent/` with the new key.
  6. Re-roll every host with the new `CADENCE_MINISIGN_PUB`.
  There is no transition window to manage: a host keeps running its installed
  agent until step 6 re-rolls it, and is never "stuck" because the operator
  re-runs the installer on each. A zero-touch multi-key rotation (installer
  accepting several keys) is only needed if hosts self-update without the
  operator, they do not.

## Release automation

A `git` tag pushed to the **public** GitHub repo triggers
`.github/workflows/release.yml` (GitHub Actions only; the private GitLab CI
stays test-only). An `agent-v*` tag builds the agent for `linux/amd64` and
`linux/arm64` (`CGO_ENABLED=0`, version from the tag), packages each into a
`.deb` with `nfpm` (`packaging/nfpm.yaml`, run from a digest-pinned image), and
attaches the binaries + `.deb`s + a `.sha256` for each to a GitHub Release; a
`v*` tag builds and pushes
`ghcr.io/johlansl/cadence-{backend,frontend}` (`linux/amd64`) after asserting
the tag matches `backend/app/__init__.py` and `frontend/package.json`, then cuts
a Release. Notes come from the matching `## <version>` section of the relevant
changelog.

- **No signing key is exposed to CI, deliberately, and this is not the old
  "CI is a build-only artifact" wording softening.** The minisign key signs
  releases *for the whole fleet*; putting it in a third-party CI on a personal
  account would make a compromised workflow dependency, a stolen repo-admin
  session, or GitHub itself enough to forge a fleet-wide agent. minisign stays
  a central-server concern (`scripts/publish-agent.sh`), covering the
  `install.sh` channel the fleet actually uses, which does **not** go through
  GitHub.
- **CI artifacts carry a Sigstore build-provenance attestation instead**
  (`actions/attest-build-provenance`, keyless via OIDC). It proves "built by
  this workflow, from this repo, at this commit", verifiable with
  `gh attestation verify <file-or-oci-ref> --repo Johlansl/cadence`, with no
  long-lived key anywhere. This is the tamper-evidence for anything pulled from
  GitHub / GHCR; minisign remains the tamper-evidence for the LAN `install.sh`
  path.
- **GHCR images are `amd64` only** for now (the server target is amd64). The
  agent is built for `arm64` too. `docker-compose.release.yml` is the overlay
  that runs the published images; the central server keeps building from source
  via `scripts/deploy.sh`.
- **The agent `.deb` is unsigned (no GPG) and is not served from an apt
  repository.** GPG package signing would be another long-lived key in CI, and
  a loose `.deb` signature is barely checked anyway (apt verifies a *repo*, not
  a file). Provenance is the same Sigstore attestation as the raw binary
  (`gh attestation verify cadence-agent_<ver>-1_<arch>.deb --repo
  Johlansl/cadence`). It is a Release asset for a local `apt install ./…deb`,
  not a `sources.list` entry, a real repository needs its own repo-signing key
  managed out of CI (like minisign) and is out of scope. `.rpm` waits for the
  agent to speak `dnf` (see "V1 scope"); the package is deliberately
  Debian/Ubuntu-shaped (it wires up the systemd units and recommends the
  reboot-required helper) but does **not** trust a site CA or write the
  per-host token, the operator still does that, exactly as with `install.sh`.

## CI

Two CI configs run the same five jobs (`agent`, `scripts`, `backend`,
`frontend`, `stack`): `.gitlab-ci.yml` on the private GitLab repo (metered
runner minutes) and `.github/workflows/ci.yml` on the public GitHub mirror
(free minutes for a public repo). They are kept behaviourally equal;
`MAINTAINING.md` "Keeping the two CI configs in sync" is the reference.

- **Path-filter every job.** A job runs only when its own tree, the CI file,
  or a coupled component's contract files change. The filter patterns are the
  **shared contract** between the two CIs; the mechanism differs (GitLab
  native `rules:changes`; GitHub a first `changes` job that diffs the range
  and a `gate` job that aggregates the results as the single required check).
  `agent` and `backend` cross-list each other's wire-contract files
  (`app/api/deps.py`, `app/schemas/schemas.py`, `internal/report/`,
  `internal/client/`, this file) so an edit to one side's contract still runs
  the other side's suite.
- **`stack` runs only on `main` and merge-request pipelines**, never a plain
  feature branch: the dind smoke is the most expensive job and its
  regressions (a broken Dockerfile, a migration that fails to bootstrap from
  `0001`) only need catching before a change lands. With the local-`--ff-only`
  workflow this is the push to `main`.
- **No redundant runs.** GitLab drops tag pipelines (nothing is tag-gated;
  releases run on GitHub) and auto-cancels a superseded pipeline
  (`interruptible`); GitHub cancels the in-flight run for a ref on a new push
  (`concurrency`, `cancel-in-progress: true`).
- **Caches are platform-specific by design** (GitLab job cache vs the
  `actions/setup-*` caches) and are not required to match.
- **Known coverage gap:** each of the agent (Go) and backend (Python) suites
  only checks its own internal consistency; nothing cross-checks the two
  implementations of the HMAC canonical string ("Authentication" above) or the
  wire JSON shapes. A coherent change to either, made with its own component's
  tests, passes CI undetected. The path-filter cross-listing does not close
  this. Tracked in `cadence-backlog.md` ("Infra / quality backlog"): a
  committed golden-vector test (recommended) or a live signed round-trip in
  the `stack` job.

## Monitoring the control plane

The host that runs the Cadence stack is **monitored like any other host but
never auto-patched by Cadence.** Give it (and the hypervisor it runs on) a
`reboot_policy` of `never`, point no schedule at it, and apply its own updates
out of band. Rationale: the box running the control plane must not restart
itself mid-job, or apply an upgrade on Cadence's own say-so and take the
scheduler / API down with it. Cadence still reports its pending updates so they
are visible, the operator acts on them manually.

## Campaigns

Roadmap item 5. The full mechanism is in
[architecture.md](architecture.md#campaigns); the decisions behind its shape:

- **No new job type.** A campaign orchestrates ordinary `apt_upgrade` jobs.
  Adding a `campaign_upgrade` type would fork the agent's execution path for
  no benefit; the campaign lives entirely server-side, and `jobs.campaign_id`
  (nullable) is all the linkage needed.
- **The engine is a function on the existing scheduler tick, not a new
  process.** It follows the pattern already established by `tick`,
  `dispatch_pending_deliveries` and the reaper: `FOR UPDATE SKIP LOCKED`, a
  session per unit of work, crash-isolation around the loop. A separate
  daemon would be one more thing to supervise on a 2 GB box for no gain.
- **Targeting is frozen at creation, never re-evaluated.** Predictability wins
  over "live" tag membership. Tag-as-policy now exists for package exclusions
  (roadmap item 6, resolved live at job-creation), but a campaign deliberately
  does not consume it: a campaign's tag filter is just a convenient way to
  *pick* the host set once, and the set stays fixed for the run.
- **Draft then explicit activate.** One create call can touch the whole fleet.
  A mandatory review step (`draft` -> `activate`) before any job is created is
  worth the extra call; a single-host job has no such blast radius and needs
  no equivalent.
- **The category -> disposition table is Python, not a column.** `skip` vs
  `halt` per `failure_category` is policy that will be tuned; keeping it in
  `app/campaigns/engine.py` means tuning it is a code change, not a migration.
  An unmapped or missing category is treated as `halt` (fail safe: stop and
  let the operator look).
- **`max_concurrency` counts `pending` + `running` campaign jobs, not just
  `running`.** Counting only `running` would let the engine create a job for
  every host in a stage on the first tick (none are running yet), defeating
  the cap.
- **A fully terminal stage still waits an observation window before the next
  one starts** (or before the campaign completes). The point of staging is to
  catch a bad upgrade on the canary before it hits everyone; advancing the
  instant the last job returns would skip that pause. Default 600 s, per
  campaign overridable, `0` allowed.
- **`orphaned` is a real terminal state.** When a halt or cancel lands while a
  sibling host's job is still in flight, that job keeps running on the agent
  and its result is still stored in `jobs`; the campaign marks the host
  `orphaned` and stops folding its outcome into the campaign counts, rather
  than pretending the job is done or leaving the row stuck at `running`
  forever.

## V1 scope

Deliberately **out of the initial version** (the data model stays extensible
for them, but there is no code):

- multi-host reboot sequencing / rollout batching
- notifications (Slack / email / webhooks)
- multi-distribution (dnf / RPM)
- multi-user authentication / RBAC

**Since added:** automatic scheduling / maintenance windows (the `schedules`
table + the `scheduler` service); package exclusion / hold lists (roadmap
item 3: global and per-host glob patterns resolved server-side, reconciled
into dpkg's hold state by the agent every `apt_upgrade` run; roadmap item 6
added a `tag` scope, a rule applying to every host carrying a tag, additive
with the other two, no priority; host tag keys and values are lowercased on
write from item 6 on, existing rows not rewritten); outbound webhooks
(roadmap item 1: one generic signed JSON feed, no per-platform formatting);
campaigns (roadmap item 5: a staged, concurrency-capped, stop-on-failure
rollout of `apt_upgrade` jobs, driven by the scheduler; see "Campaigns"
below and [architecture.md](architecture.md#campaigns)). The `apt_upgrade`
rollout-batching line above is now largely covered by campaigns; reboot
sequencing still is not.
