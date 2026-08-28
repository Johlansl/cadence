#!/bin/sh
# Install the Cadence agent and its systemd timer on a Debian/Ubuntu host.
#
# Run as root from the agent/ directory, after building the binary:
#   CGO_ENABLED=0 go build -trimpath -o bin/cadence-agent ./cmd/agent
#   sudo systemd/install.sh [path-to-binary]
#
# Re-running is safe: it refreshes the binary and unit files but never
# overwrites an existing /etc/cadence/agent.env.

set -eu

BIN="${1:-bin/cadence-agent}"
PREFIX=/usr/local/bin
CONFDIR=/etc/cadence
UNITDIR=/etc/systemd/system
DOCDIR=/usr/local/share/doc/cadence

if [ "$(id -u)" != 0 ]; then
	echo "install.sh: must run as root" >&2
	exit 1
fi
if [ ! -f "$BIN" ]; then
	echo "install.sh: binary not found: $BIN (build it first)" >&2
	exit 1
fi

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

install -m 0755 "$BIN" "$PREFIX/cadence-agent"

install -d -m 0755 "$CONFDIR"
if [ ! -f "$CONFDIR/agent.env" ]; then
	install -m 0600 "$HERE/agent.env.example" "$CONFDIR/agent.env"
	NEEDS_CONFIG=1
fi

install -m 0644 "$HERE/cadence-agent.service" "$UNITDIR/cadence-agent.service"
install -m 0644 "$HERE/cadence-agent.timer" "$UNITDIR/cadence-agent.timer"

if [ -f "$HERE/../../README.md" ]; then
	install -D -m 0644 "$HERE/../../README.md" "$DOCDIR/README.md"
fi

systemctl daemon-reload
systemctl enable --now cadence-agent.timer

echo
if [ "${NEEDS_CONFIG:-0}" = 1 ]; then
	echo ">> Edit $CONFDIR/agent.env (server URL + per-host token) before the first run."
fi
echo ">> Timer:    systemctl list-timers cadence-agent.timer"
echo ">> Run now:  systemctl start cadence-agent.service && journalctl -u cadence-agent -n 20 --no-pager"
