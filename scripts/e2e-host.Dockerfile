# E2E upgrade-test host: a minimal Debian box that runs REAL systemd
# (timers, oneshots, journal) so the agent upgrade path executes exactly
# like on a production host. Used only by
# scripts/test-agent-upgrade-e2e.sh, never shipped.
#
# Run privileged with the host cgroupfs mounted and a shared cgroup
# namespace, e.g.:
#   docker run -d --privileged --cgroupns=host \
#     -v /sys/fs/cgroup:/sys/fs/cgroup:rw -e container=docker ...
FROM debian:bookworm-slim

ENV container=docker

RUN apt-get update \
    && apt-get install -y --no-install-recommends systemd systemd-sysv dbus \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /lib/systemd/system/apt-daily.* /lib/systemd/system/dpkg-db-backup.* \
    && systemctl mask apt-daily.timer apt-daily-upgrade.timer dpkg-db-backup.timer systemd-tmpfiles-clean.timer

STOPSIGNAL SIGRTMIN+3
CMD ["/lib/systemd/systemd"]
