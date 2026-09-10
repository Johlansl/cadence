# Cadence agent: changelog

The agent reports its version to the server on every report; `cadence-agent
-version` prints it.

## 0.12.1

Drop `RestrictSUIDSGID=true` from `cadence-agent.service` and
`cadence-agent-poll.service`. The directive blocked the agent process from
creating or changing setuid/setgid files, which silently broke legitimate
package upgrades that ship such files: `shadow` installing `newgrp` / `chage`
setuid root, for instance, failed under the agent while the same `dpkg` run by
hand succeeded. Reliability of the upgrade path wins over this layer of
defence in depth here; the other sandboxing directives (`ProtectHome`,
`PrivateTmp`, `ProtectControlGroups`, `LockPersonality`) stay. See
`SECURITY.md`, "The agent runs as root", for the residual-risk rationale. No
behaviour change in the agent binary itself.

## 0.12.0

Pre- and post-upgrade health checks (server roadmap item 7):

- Before an `apt_upgrade`, the agent verifies free space on the filesystems
  backing `/var`, `/boot` and `/boot/efi`, waits for apt/dpkg advisory locks,
  runs `dpkg --audit` and `apt-get check`, records the existing failed systemd
  services, and refreshes package indexes with strict error handling. A failed
  or unavailable blocking check stops the upgrade and maps to the existing job
  failure categories. The checks do not repair packages or restart services.
- Once the pre-check phase passes, post-checks run after the upgrade action
  path even when that action fails. They repeat the dpkg, apt and disk checks,
  compare failed services with the baseline, and record whether a reboot is
  required. The job action outcome and the host health outcome are separate:
  apt can succeed while the host is `unhealthy`, or fail while post-check
  evidence is still available.
- The structured `pre_checks`, `post_checks` and derived `health_status`
  (`healthy`, `degraded`, `unhealthy`, `unknown`) are submitted with the job
  result. Check evidence is bounded and the command environment remains
  stripped of every `CADENCE_*` secret.
- Disk and lock thresholds come from the job when a current server supplies
  them. Jobs created by an older server use the same local defaults: 1 GiB for
  `/var`, 200 MiB for boot filesystems, and a 120 second lock wait.
- Job execution now has a 60 minute overall deadline and the systemd unit
  limits were raised accordingly so the pre-check, action, post-check and
  follow-up report sequence cannot be killed prematurely.

## 0.11.0

Dry-run (server roadmap item 4): a new `apt_dry_run` job type that previews
what an `apt_upgrade` would do without changing anything on the host.

- `RunAptDryRun` refreshes the package lists, runs `apt-get -s
  dist-upgrade`, and parses the simulation into a structured result:
  packages that would be upgraded, newly installed (dependencies apt would
  pull in), or removed, plus the ones apt keeps back. It never runs
  `apt-mark`, `dpkg`, or a real upgrade; all three commands it does run
  (`apt-get update`, `apt-get -s dist-upgrade`, `apt-mark showhold`) have
  fixed arguments.
- The job's `params.excluded_packages` (resolved server-side, exactly as
  for `apt_upgrade`) is used only to filter the parsed result: a name
  matching an active exclusion is moved to `dry_run.excluded`, even if apt
  has not been told to hold it yet. Separately, `dry_run.held_in_place`
  lists packages apt kept back because of a hold already on the box,
  correlated the same way `held_conflicts` is on a real upgrade.
- The dispatch runs a dry-run even when `CADENCE_ENABLE_UPGRADES=false`:
  it is a pure read, and previewing pending changes on an upgrade-disabled
  host is useful.
- The `apt-get -s dist-upgrade` line parsing that the periodic report
  collector already used is now a shared `internal/aptsim` package, so the
  report path and the dry-run path cannot drift apart.

## 0.10.1

Bug fix: `0.10.0`'s hold reconciliation diffed against a live `apt-mark
showhold` read, so it unheld *every* package not in that run's
`excluded_packages` -- including ones held by something other than Cadence
(an operator, unattended-upgrades, a distro default). Confirmed live on a
real host: two long-standing third-party holds were lifted, one of the
freed packages then hit an unrelated dpkg permission error during its
upgrade.

Reconciliation now diffs against `params.known_held_packages`, the set the
server last recorded Cadence itself holding on that host (from the
previous `apt_upgrade` job's `held_packages` result), never against
`apt-mark showhold` directly. `apt-mark showhold` is still read, but only
to log an informational count in the job log. A hold in neither
`known_held_packages` nor `excluded_packages` -- a third party's -- is now
left untouched. The job result carries `held_packages`, the set actually
held after reconciliation, with the same never-omitted convention as
`held_conflicts`, so the server can track it forward to the next job.

## 0.10.0

Package exclusion / hold policies (server roadmap item 3):

- A new `internal/holds` package validates the package names an
  `apt_upgrade` job carries in `params.excluded_packages` (a strict Debian
  package-name format; an invalid name is logged and dropped, never passed
  to a command), reconciles dpkg's real hold state to them every run
  (`apt-mark hold`/`unhold`, diffed against `apt-mark showhold` so an
  orphaned hold from an interrupted or superseded run never lingers), and
  correlates dist-upgrade's own output back to the exact names this agent
  held.
- `RunAptUpgrade` now reconciles holds between refreshing the package lists
  and running dist-upgrade. The job result carries `held_conflicts`: nil
  when not applicable, an empty list when reconciliation ran cleanly, and
  the held package name(s) apt showed real evidence of skipping or blocking
  on ("kept back", or a dependency conflict) otherwise. Unlike
  `failure_category`/`failure_summary`, this field is never omitted from
  the result, even on success: the null/empty/non-empty distinction is the
  point.
- The server resolves patterns to exact names before they ever reach the
  agent; the pattern itself is never sent.

## 0.9.0

Failure classification on a failed job (server roadmap item 2):

- `internal/apterr` gains disk-full, network/mirror and dpkg-error detection
  on top of the held-lock and interrupted-state patterns it already had, plus
  a `Classify` that maps the output of the command that failed to one coarse
  category (`apt_locked`, `network_or_repo`, `dpkg_error`, `disk_full`,
  `timeout`, `unknown`) and a `Summary` that lifts the single most useful
  line out of the output.
- The job result now carries `failure_category` and `failure_summary` for a
  failed job. The executor classifies the isolated output of the failing
  `dist-upgrade` attempt (so an earlier retry's lock message cannot mislabel
  it), falls back to the full log, and reports `timeout` when the job's own
  deadline expired. A job the agent declines before running anything
  (upgrades disabled, reboot disabled, unsupported job type) reports
  `agent_refused`. Both fields are omitted from a successful result, so the
  wire form of a success is unchanged.
- Needs a server that stores the two fields (shipped alongside this); an
  older server simply ignores them.

## 0.8.0

Signed requests, replacing the raw bearer token on every call:

- `internal/client` now signs each POST instead of sending
  `Authorization: Bearer <token>`. Three headers carry the SHA-256 of the
  token (a non-secret lookup key, the same value the server already stores
  as `token_hash`, not the raw secret), a Unix timestamp, and an
  HMAC-SHA256 over `timestamp\nMETHOD\npath\nsha256(body)` keyed with the
  token. Re-signed fresh on every retry attempt, not just once per call.
  `CADENCE_TOKEN` is unchanged, still the one secret used both ways.
- Needs a server that understands the new headers (this server-side work
  shipped ahead of this tag) *and* a token issued or rotated after that
  server-side change -- a token from before then has no way to verify a
  signature and this binary cannot fall back to the old bearer form. Rotate
  the host's token before upgrading it to `0.8.0`, not after: see
  `docs/decisions.md` "Authentication" for the exact per-host order.
- No other runtime change.

## 0.7.1

No runtime change, the binary is byte-for-byte `0.7.0` behaviour. This tag is
the first release cut through `.github/workflows/release.yml`: `linux/arm64`
binaries alongside `linux/amd64`, each with its SHA-256 and a Sigstore
build-provenance attestation, published to a GitHub Release.

## 0.7.0

Advisory linkage:

- collector: each package now carries its Debian source package name
  (`dpkg-query ${source:Package}`) and the report carries the OS release
  codename (`/etc/os-release` `VERSION_CODENAME`). The server uses them to link
  Debian security advisories precisely, a library binary such as `libssl3` is
  matched via its source (`openssl`), and a release outside the server's
  built-in `VERSION_ID` list still resolves. Both are additive JSON fields
  (`source_package`, `os_codename`); an older server ignores them and keeps
  accepting pre-`0.7.0` reports unchanged.

## 0.6.2

Security:

- The `CADENCE_*` variables (`CADENCE_TOKEN` among them) are stripped from the
  environment of every command the agent runs, `apt-get`, `dpkg-query`,
  `dpkg`, `systemctl`, `shutdown`. They were inherited by those children,
  exposing the host token via `/proc/<pid>/environ`, apt hooks and dpkg
  maintainer scripts. New `internal/procenv` helper; no behaviour change for a
  healthy host.

## 0.6.1

Robustness (no behaviour change for a healthy host):

- `systemd`: `cadence-agent.service` `TimeoutStartSec` 600 -> 3600 and
  `cadence-agent-poll.service` 1800 -> 2700, so systemd no longer SIGKILLs
  apt/dpkg mid `dist-upgrade` (the run's own timeouts are the real bound).
- executor: `dpkg --configure -a` recovery runs under a fresh context (it was a
  no-op when the upgrade failed *because* its deadline expired); `apt-get
  dist-upgrade` is retried while another process holds the apt lock; apt/dpkg
  get SIGTERM-then-SIGKILL on cancel so an orphaned child can't wedge the run.
- collector: a half-configured dpkg state is repaired once (`dpkg --configure
  -a`) instead of failing every report forever; only packages with
  `${db:Status-Status}` == `installed` are reported (excludes `config-files`).
- job log is capped at 128 KiB before submit.
- reboot: falls back to `shutdown -r now` when `systemctl` is absent/fails.
- `cadence-agent.timer`: dropped the inert `Persistent=true` (monotonic timer).

## 0.6.0

- Detect a pending kernel reboot without `update-notifier-common` (compare the
  running `uname -r` against installed `linux-image-*`).
- `apt-get update` before a job's `dist-upgrade`; `dpkg --configure -a` on
  failure; retry on a held apt lock during collection; bounded retry/backoff in
  the HTTP client.
- Structured (logfmt) logging.
