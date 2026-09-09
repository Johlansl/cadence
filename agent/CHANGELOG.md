# Cadence agent: changelog

The agent reports its version to the server on every report; `cadence-agent
-version` prints it.

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
