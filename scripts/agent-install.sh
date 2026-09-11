#!/bin/sh
# Cadence agent installer. The trusted bootstrap prelude downloads this file
# only after pinning the server CA from the manually transferred enrollment
# code. It must never be executed from an unauthenticated HTTP response.

set -eu

base=${CADENCE_BASE_URL:-https://cadence.lan}
verified_ca=${CADENCE_VERIFIED_CA:-}
case "$base" in
https://*) ;;
*) echo "install.sh: CADENCE_BASE_URL must use https" >&2; exit 2 ;;
esac
if [ "$(id -u)" != "0" ]; then
	echo "install.sh: run as root (sudo)" >&2
	exit 1
fi
if [ -z "$verified_ca" ] || [ ! -f "$verified_ca" ]; then
	echo "install.sh: CADENCE_VERIFIED_CA must name the fingerprint-verified CA" >&2
	exit 1
fi

temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT HUP INT TERM

fetch() {
	curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 \
		--cacert "$verified_ca" "$base/$1" -o "$2"
}

echo "install.sh: authenticated server $base"

# Download and verify every artifact before changing the host. HTTPS provides
# origin authentication; sha256 still detects truncation or staging mistakes.
fetch agent/cadence-agent "$temporary/cadence-agent"
fetch agent/cadence-agent.sha256 "$temporary/cadence-agent.sha256"
want=$(awk '{print $1}' "$temporary/cadence-agent.sha256")
got=$(sha256sum "$temporary/cadence-agent" | awk '{print $1}')
if [ "$want" != "$got" ]; then
	echo "install.sh: checksum mismatch (want $want, got $got)" >&2
	exit 1
fi

pub=${CADENCE_MINISIGN_PUB:-}
if [ -n "$pub" ]; then
	if ! command -v minisign >/dev/null 2>&1; then
		echo "install.sh: CADENCE_MINISIGN_PUB is set but minisign is not installed" >&2
		exit 1
	fi
	fetch agent/cadence-agent.minisig "$temporary/cadence-agent.minisig"
	if [ -f "$pub" ]; then set -- -p "$pub"; else set -- -P "$pub"; fi
	if ! minisign -V "$@" -m "$temporary/cadence-agent" \
		-x "$temporary/cadence-agent.minisig" >/dev/null 2>&1; then
		echo "install.sh: minisign signature invalid" >&2
		exit 1
	fi
	echo "install.sh: minisign signature OK"
else
	echo "install.sh: authenticated download and sha256 OK"
fi

for unit in cadence-agent.service cadence-agent.timer \
	cadence-agent-poll.service cadence-agent-poll.timer \
	cadence-agent-health-check-boot.service cadence-agent-health-check-boot.timer; do
	fetch "agent/systemd/$unit" "$temporary/$unit"
done

# reboot-required flag support. Best effort: the agent remains usable without
# the distro helper, only reboot-required detection degrades.
if dpkg -s update-notifier-common >/dev/null 2>&1 ||
	dpkg -s reboot-notifier >/dev/null 2>&1; then
	:
else
	apt-get update -qq >/dev/null 2>&1 || true
	helper=""
	for candidate in update-notifier-common reboot-notifier; do
		if apt-cache show "$candidate" >/dev/null 2>&1; then helper=$candidate; break; fi
	done
	if [ -n "$helper" ] && DEBIAN_FRONTEND=noninteractive \
		apt-get install -y -qq "$helper" >/dev/null 2>&1; then
		echo "install.sh: $helper installed (reboot-required detection)"
	else
		echo "install.sh: WARNING no reboot-required helper available; detection may degrade" >&2
	fi
fi

server_ca=/usr/local/share/ca-certificates/cadence-server.crt
install -m 0644 "$verified_ca" "$server_ca"
update-ca-certificates >/dev/null 2>&1
install -m 0755 "$temporary/cadence-agent" /usr/local/bin/cadence-agent
for unit in cadence-agent.service cadence-agent.timer \
	cadence-agent-poll.service cadence-agent-poll.timer \
	cadence-agent-health-check-boot.service cadence-agent-health-check-boot.timer; do
	install -m 0644 "$temporary/$unit" "/etc/systemd/system/$unit"
done

# The code arrives on stdin, never argv or the environment. The binary creates
# the private key locally and switches agent.env only after all credentials
# have been validated and written.
/usr/local/bin/cadence-agent -enroll -enroll-server "$base" \
	-enroll-ca "$server_ca" -enroll-directory /etc/cadence
echo "install.sh: agent $(/usr/local/bin/cadence-agent -version 2>/dev/null || echo '(installed)')"

systemctl daemon-reload
systemctl enable --now cadence-agent.timer cadence-agent-poll.timer \
	cadence-agent-health-check-boot.timer >/dev/null
echo "install.sh: timers enabled. First report:"
systemctl start cadence-agent.service || true
echo "  journalctl -u cadence-agent -n 20 --no-pager"
