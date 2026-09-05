# Cadence agent — changelog

The agent reports its version to the server on every report; `cadence-agent
-version` prints it.

## 0.7.0

Advisory linkage:

- collector: each package now carries its Debian source package name
  (`dpkg-query ${source:Package}`) and the report carries the OS release
  codename (`/etc/os-release` `VERSION_CODENAME`). The server uses them to link
  Debian security advisories precisely — a library binary such as `libssl3` is
  matched via its source (`openssl`), and a release outside the server's
  built-in `VERSION_ID` list still resolves. Both are additive JSON fields
  (`source_package`, `os_codename`); an older server ignores them and keeps
  accepting pre-`0.7.0` reports unchanged.

## 0.6.2

Security:

- The `CADENCE_*` variables (`CADENCE_TOKEN` among them) are stripped from the
  environment of every command the agent runs — `apt-get`, `dpkg-query`,
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
