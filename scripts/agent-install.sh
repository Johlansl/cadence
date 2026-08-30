#!/bin/sh
# Cadence agent installer -- fetched from the central server and piped to sh:
#
#   curl -fsSL http://cadence.lan/install.sh | sudo CADENCE_TOKEN=<token> sh
#
# Run as root on a Debian/Ubuntu VM. It trusts the server's internal CA,
# installs update-notifier-common, drops the agent binary + systemd units,
# writes /etc/cadence/agent.env, and starts the timers.
#
# Environment:
#   CADENCE_TOKEN       agent token from scripts/provision-host.sh (required
#                       unless /etc/cadence/agent.env already has one)
#   CADENCE_BASE_URL    where to fetch install assets from
#                       (default: http://cadence.lan)
#   CADENCE_SERVER_URL  HTTPS API URL written into agent.env
#                       (default: https://<host of CADENCE_BASE_URL>)

set -eu

base=${CADENCE_BASE_URL:-http://cadence.lan}
host=$(printf '%s\n' "$base" | sed -E 's#^[a-z]+://##; s#/.*##')
server_url=${CADENCE_SERVER_URL:-https://$host}
token=${CADENCE_TOKEN:-}

if [ "$(id -u)" != "0" ]; then
	echo "install.sh: run as root (sudo)" >&2
	exit 1
fi

fetch() { curl -fsSL "$base/$1"; }

echo "install.sh: server $base"

# 1. Trust the internal CA.
tmp_ca=$(mktemp)
fetch agent/ca.crt >"$tmp_ca"
install -m 0644 "$tmp_ca" /usr/local/share/ca-certificates/cadence-internal.crt
rm -f "$tmp_ca"
update-ca-certificates >/dev/null
echo "install.sh: CA installed"

# 2. reboot-required flag support (Debian minimal lacks it).
if ! dpkg -s update-notifier-common >/dev/null 2>&1; then
	apt-get update -qq
	DEBIAN_FRONTEND=noninteractive apt-get install -y -qq update-notifier-common >/dev/null
	echo "install.sh: update-notifier-common installed"
fi

# 3. Agent binary, checksum-verified.
tmp_bin=$(mktemp)
fetch agent/cadence-agent >"$tmp_bin"
want=$(fetch agent/cadence-agent.sha256 | awk '{print $1}')
got=$(sha256sum "$tmp_bin" | awk '{print $1}')
if [ "$want" != "$got" ]; then
	echo "install.sh: checksum mismatch (want $want, got $got)" >&2
	rm -f "$tmp_bin"
	exit 1
fi
install -m 0755 "$tmp_bin" /usr/local/bin/cadence-agent
rm -f "$tmp_bin"
echo "install.sh: agent $(/usr/local/bin/cadence-agent -version 2>/dev/null || echo '(installed)')"

# 4. systemd units.
for unit in cadence-agent.service cadence-agent.timer \
	cadence-agent-poll.service cadence-agent-poll.timer; do
	fetch "agent/systemd/$unit" >"/etc/systemd/system/$unit"
	chmod 0644 "/etc/systemd/system/$unit"
done

# 5. Config. Never clobber an existing token.
mkdir -p /etc/cadence
if [ ! -f /etc/cadence/agent.env ]; then
	if [ -z "$token" ]; then
		echo "install.sh: no CADENCE_TOKEN and no /etc/cadence/agent.env" >&2
		echo "  provision the host first (scripts/provision-host.sh) and re-run with CADENCE_TOKEN=..." >&2
		exit 1
	fi
	umask 077
	cat >/etc/cadence/agent.env <<EOF
CADENCE_SERVER_URL=$server_url
CADENCE_TOKEN=$token
EOF
	echo "install.sh: wrote /etc/cadence/agent.env"
elif [ -n "$token" ]; then
	echo "install.sh: /etc/cadence/agent.env exists, keeping its token"
fi

# 6. Enable + start.
systemctl daemon-reload
systemctl enable --now cadence-agent.timer cadence-agent-poll.timer >/dev/null
echo "install.sh: timers enabled. First report:"
systemctl start cadence-agent.service || true
echo "  journalctl -u cadence-agent -n 20 --no-pager"
