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
#   CADENCE_MINISIGN_PUB  release public key (the "RW..." line, or a path to a
#                       file holding it), distributed out of band. When set,
#                       the binary's minisign signature is verified and a
#                       failure aborts the install.

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

# 1. Trust the internal CA (skip if an identical cert is already trusted, to
#    avoid update-ca-certificates "duplicate" noise on re-runs).
tmp_ca=$(mktemp)
fetch agent/ca.crt >"$tmp_ca"
if find /usr/local/share/ca-certificates -name '*.crt' \
	-exec cmp -s "$tmp_ca" {} \; -print 2>/dev/null | grep -q .; then
	echo "install.sh: CA already trusted"
else
	install -m 0644 "$tmp_ca" /usr/local/share/ca-certificates/cadence-internal.crt
	update-ca-certificates >/dev/null 2>&1
	echo "install.sh: CA installed"
fi
rm -f "$tmp_ca"

# 2. reboot-required flag support. update-notifier-common (Ubuntu, older Debian)
#    or reboot-notifier (Debian 13+, update-notifier-common was dropped) both
#    ship the apt/kernel hooks that create /var/run/reboot-required. Best-effort:
#    the agent works without it, only reboot-required detection degrades.
if dpkg -s update-notifier-common >/dev/null 2>&1 ||
	dpkg -s reboot-notifier >/dev/null 2>&1; then
	:  # a helper is already installed
else
	apt-get update -qq >/dev/null 2>&1 || true
	helper=""
	for cand in update-notifier-common reboot-notifier; do
		if apt-cache show "$cand" >/dev/null 2>&1; then helper=$cand; break; fi
	done
	if [ -n "$helper" ] && DEBIAN_FRONTEND=noninteractive \
		apt-get install -y -qq "$helper" >/dev/null 2>&1; then
		echo "install.sh: $helper installed (reboot-required detection)"
	else
		echo "install.sh: WARNING no reboot-required helper available --" \
			"reboot-required detection may not work on this host" >&2
	fi
fi

# 3. Agent binary. Always sha256-checked (guards against a truncated download).
#    Also minisign-verified when you pass the release public key out of band in
#    CADENCE_MINISIGN_PUB (the key line itself, or a path to it) -- that is the
#    only check that resists tampering on this plain-HTTP channel.
tmp_bin=$(mktemp)
fetch agent/cadence-agent >"$tmp_bin"
want=$(fetch agent/cadence-agent.sha256 | awk '{print $1}')
got=$(sha256sum "$tmp_bin" | awk '{print $1}')
if [ "$want" != "$got" ]; then
	echo "install.sh: checksum mismatch (want $want, got $got)" >&2
	rm -f "$tmp_bin"
	exit 1
fi

pub=${CADENCE_MINISIGN_PUB:-}
if [ -n "$pub" ]; then
	if ! command -v minisign >/dev/null 2>&1; then
		echo "install.sh: CADENCE_MINISIGN_PUB is set but minisign is not installed" >&2
		echo "  run 'apt-get install minisign', or unset it to rely on sha256 only" >&2
		rm -f "$tmp_bin"
		exit 1
	fi
	tmp_sig=$(mktemp)
	if ! fetch agent/cadence-agent.minisig >"$tmp_sig" 2>/dev/null; then
		echo "install.sh: the server has no cadence-agent.minisig (release not signed)" >&2
		rm -f "$tmp_bin" "$tmp_sig"
		exit 1
	fi
	if [ -f "$pub" ]; then set -- -p "$pub"; else set -- -P "$pub"; fi
	if minisign -V "$@" -m "$tmp_bin" -x "$tmp_sig" >/dev/null 2>&1; then
		echo "install.sh: minisign signature OK"
	else
		echo "install.sh: minisign signature INVALID -- refusing to install" >&2
		rm -f "$tmp_bin" "$tmp_sig"
		exit 1
	fi
	rm -f "$tmp_sig"
else
	echo "install.sh: sha256 OK (set CADENCE_MINISIGN_PUB to also check the signature)"
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
CADENCE_RUN_APT_UPDATE=true
EOF
	echo "install.sh: wrote /etc/cadence/agent.env"
else
	[ -n "$token" ] && echo "install.sh: /etc/cadence/agent.env exists, keeping its token"
	# Make sure apt lists are refreshed before each report.
	if ! grep -q '^CADENCE_RUN_APT_UPDATE=' /etc/cadence/agent.env; then
		echo 'CADENCE_RUN_APT_UPDATE=true' >>/etc/cadence/agent.env
		echo "install.sh: added CADENCE_RUN_APT_UPDATE=true to agent.env"
	fi
fi

# 6. Enable + start.
systemctl daemon-reload
systemctl enable --now cadence-agent.timer cadence-agent-poll.timer >/dev/null
echo "install.sh: timers enabled. First report:"
systemctl start cadence-agent.service || true
echo "  journalctl -u cadence-agent -n 20 --no-pager"
