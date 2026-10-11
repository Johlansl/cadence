#!/bin/sh
# Cadence agent installer. The trusted bootstrap prelude downloads this file
# only after pinning the server CA from the manually transferred enrollment
# code. It must never be executed from an unauthenticated HTTP response.
#
# Optional release-signature check (minisign must be installed on the host):
#   CADENCE_MINISIGN_PUB   trusted public key: a key file (comment line, then
#                          key line) or a bare key (RW + 54 base64 characters)
#   CADENCE_MINISIGN_PUB2  optional second trusted key (the offline backup
#                          key), same format; requires CADENCE_MINISIGN_PUB
# The signature is accepted if either key verifies it. Both values must be
# well formed even when the first key verifies. Keys are never fetched.
#
# CADENCE_UPGRADE_ONLY=true additionally refuses to install a release older
# than what the host already runs, with no way around it:
#   - CADENCE_MINISIGN_PUB (and so minisign) is mandatory, because the version
#     of the downloaded binary is only trusted, and the binary only executed,
#     after its signature has been verified;
#   - the floor is the highest version reported by -version among the two
#     agent paths this installer has ever used (the canonical one and the
#     historical one); a path that exists but cannot be read as an agent
#     (symlink, directory, not executable, unreadable version) refuses;
#   - no agent binary at either path refuses.
# Versions are compared on their three leading integers only: 0.15.0-3-gabc
# counts as 0.15.0, and an equal version is accepted (reinstall).

set -eu

base=${CADENCE_BASE_URL:-https://cadence.lan}
verified_ca=${CADENCE_VERIFIED_CA:-}
upgrade_only=${CADENCE_UPGRADE_ONLY:-false}
canon_bin=/usr/bin/cadence-agent
legacy_bin=/usr/local/bin/cadence-agent
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
case "$upgrade_only" in
true)
	if [ ! -f /etc/cadence/agent.env ] ||
		! grep -q '^CADENCE_CLIENT_CERT_FILE=' /etc/cadence/agent.env; then
		echo "install.sh: CADENCE_UPGRADE_ONLY requires an enrolled agent" >&2
		exit 1
	fi
	;;
false) ;;
*) echo "install.sh: CADENCE_UPGRADE_ONLY must be true or false" >&2; exit 2 ;;
esac

# --- minisign trust set ------------------------------------------------------

# is_key <string>: exactly a minisign public key line, ^RW[A-Za-z0-9+/]{54}$.
# (Explicit character list: a range like A-Z is locale dependent in some shells.)
is_key() {
	case $1 in
	RW*) ;;
	*) return 1 ;;
	esac
	case $1 in
	*[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/]*) return 1 ;;
	esac
	[ "${#1}" -eq 56 ]
}

# check_pub <variable name> <value>: fail closed unless <value> is exactly a
# minisign public key in one of the two accepted forms: a key file (exactly two
# lines, "untrusted comment: ..." then a key line) or a bare key. Only the shape
# is checked here; minisign still parses the key when it verifies.
check_pub() {
	if [ -f "$2" ]; then
		_lines=$(sed -n '$=' "$2" 2>/dev/null) || _lines=""
		_comment=$(sed -n 1p "$2" 2>/dev/null) || _comment=""
		_key=$(sed -n 2p "$2" 2>/dev/null) || _key=""
		case $_comment in
		"untrusted comment:"*) ;;
		*) _lines="" ;;
		esac
		if [ "$_lines" = "2" ] && is_key "$_key"; then return 0; fi
	elif is_key "$2"; then
		return 0
	fi
	echo "install.sh: $1 must be a minisign public key file (comment line, then key line) or a bare key (RW + 54 base64 characters)" >&2
	return 1
}

# pub_key <value>: the bare key behind a validated value.
pub_key() {
	if [ -f "$1" ]; then sed -n 2p "$1" 2>/dev/null || true; else printf '%s\n' "$1"; fi
}

# key_id <base64>: the 16-hex key ID carried by a public key line or a
# signature line (bytes 3 to 10 once decoded, shown in reverse order). Purely
# informational and never able to fail the script: any missing tool or error
# prints "?". The ID of a signature is what the signer CLAIMS; only a
# verification means anything.
key_id() {
	_id=$(printf '%s' "$1" | base64 -d 2>/dev/null | od -An -tx1 -j2 -N8 2>/dev/null |
		tr -d ' \n' |
		sed 's/^\(..\)\(..\)\(..\)\(..\)\(..\)\(..\)\(..\)\(..\)$/\8\7\6\5\4\3\2\1/' |
		tr 'a-f' 'A-F') || _id=""
	case $_id in
	"" | *[!0-9A-F]*) _id='?' ;;
	esac
	[ "${#_id}" -eq 16 ] || _id='?'
	printf '%s\n' "$_id"
}

# try_key <value>: succeed when this key verifies the downloaded binary. Used
# as an `if` condition only, so it never relies on `set -e`. Passing the key as
# -p <file> or -P <key> means no key material is ever written to a temp file.
try_key() {
	if [ -f "$1" ]; then set -- -p "$1"; else set -- -P "$1"; fi
	minisign -V "$@" -m "$temporary/cadence-agent" \
		-x "$temporary/cadence-agent.minisig" >/dev/null 2>&1
}

# --- downgrade check (CADENCE_UPGRADE_ONLY=true) -----------------------------

# version_triple <string>: succeed, and set v_maj v_min v_pat, only for a
# strict canonical N.N.N: digits and dots, three components, each "0" or
# starting with 1-9, at most 9 digits (shell integer tests stay exact). Same
# grammar as check_version in scripts/publish-agent.sh and ParseTarget in the
# agent; scripts/test-ci-sign-agent.sh keeps the copies in step.
version_triple() {
	case $1 in
	*[!0-9.]* | "" | .* | *. | *..*) return 1 ;;
	esac
	v_maj=${1%%.*}
	_rest=${1#*.}
	case $_rest in *.*) ;; *) return 1 ;; esac
	v_min=${_rest%%.*}
	v_pat=${_rest#*.}
	case $v_pat in *.*) return 1 ;; esac
	for _c in "$v_maj" "$v_min" "$v_pat"; do
		case $_c in
		"") return 1 ;;
		0) ;;
		0*) return 1 ;;
		esac
		[ "${#_c}" -le 9 ] || return 1
	done
}

# version_current <string>: the -version of an installed agent, as the agent
# itself reads it (ParseCurrent): N.N.N, optionally followed by "-" and a
# non-empty suffix (git describe builds look like 0.14.0-51-ge946197). Only
# the three integers count; the suffix is dropped.
version_current() {
	[ "${#1}" -le 316 ] || return 1
	case $1 in
	*-*)
		[ -n "${1#*-}" ] || return 1
		version_triple "${1%%-*}"
		;;
	*) version_triple "$1" ;;
	esac
}

# triple_gt <a1> <a2> <a3> <b1> <b2> <b3>: succeed when a is strictly newer.
triple_gt() {
	[ "$1" -gt "$4" ] && return 0
	[ "$1" -lt "$4" ] && return 1
	[ "$2" -gt "$5" ] && return 0
	[ "$2" -lt "$5" ] && return 1
	[ "$3" -gt "$6" ]
}

# probe_version <binary>: run "<binary> -version" and set probe_text to its
# single output line. This is the only place an agent binary is executed:
# stdin closed, stderr dropped, empty environment, killed after 30 seconds,
# output cut at 4096 bytes. On any failure it sets probe_err and fails.
probe_version() {
	probe_err=""
	probe_text=""
	rm -f "$temporary/probe.rc" "$temporary/probe.out"
	{
		_rc=0
		timeout -s KILL 30 env -i "$1" -version </dev/null 2>/dev/null || _rc=$?
		printf '%s\n' "$_rc" >"$temporary/probe.rc"
	} | head -c 4097 >"$temporary/probe.out"
	_rc=$(cat "$temporary/probe.rc" 2>/dev/null) || _rc=""
	if [ "$_rc" != "0" ]; then
		probe_err="exit status ${_rc:-unknown}"
		return 1
	fi
	_size=$(wc -c <"$temporary/probe.out" | tr -d ' ')
	if [ "$_size" -gt 4096 ]; then
		probe_err="output longer than 4096 bytes"
		return 1
	fi
	probe_text=$(cat "$temporary/probe.out")
	# exactly one line: no newline inside the text, and the text plus its own
	# newline is the whole output (so no extra blank line, no missing newline)
	_nl='
'
	case $probe_text in
	*"$_nl"*)
		probe_err="output is not exactly one line"
		return 1
		;;
	esac
	if [ "$_size" -ne $((${#probe_text} + 1)) ]; then
		probe_err="output is not exactly one line"
		return 1
	fi
}

floor_found=0
floor_maj=0
floor_min=0
floor_pat=0

# read_floor <path>: fold the version of an installed agent into the floor.
# A missing path is skipped; a path that exists but cannot be trusted as an
# agent refuses the whole run (fail closed, never skipped).
read_floor() {
	if [ ! -e "$1" ] && [ ! -L "$1" ]; then return 0; fi
	if [ -L "$1" ] || [ ! -f "$1" ] || [ ! -x "$1" ]; then
		echo "install.sh: $1 exists but is not a regular executable file (symlink, directory or not executable): refusing, the running version cannot be established. Recovery: docs/agent-path-migration.md, section 6" >&2
		exit 1
	fi
	if ! probe_version "$1"; then
		echo "install.sh: cannot read the version of $1 ($probe_err): refusing. Recovery: docs/agent-path-migration.md, section 6" >&2
		exit 1
	fi
	if ! version_current "$probe_text"; then
		echo "install.sh: $1 does not report a valid version: refusing. Recovery: docs/agent-path-migration.md, section 6" >&2
		exit 1
	fi
	if [ "$floor_found" -eq 0 ] ||
		triple_gt "$v_maj" "$v_min" "$v_pat" "$floor_maj" "$floor_min" "$floor_pat"; then
		floor_maj=$v_maj
		floor_min=$v_min
		floor_pat=$v_pat
	fi
	floor_found=1
}

# Shape checks come first, before anything is downloaded, and apply to both
# keys even when the first one would verify: a malformed second key is a
# configuration error, never silently ignored.
pub=${CADENCE_MINISIGN_PUB:-}
pub2=${CADENCE_MINISIGN_PUB2:-}
if [ -z "$pub" ] && [ -n "$pub2" ]; then
	echo "install.sh: CADENCE_MINISIGN_PUB2 requires CADENCE_MINISIGN_PUB" >&2
	exit 1
fi
if [ -n "$pub" ]; then
	check_pub CADENCE_MINISIGN_PUB "$pub" || exit 1
fi
if [ -n "$pub2" ]; then
	check_pub CADENCE_MINISIGN_PUB2 "$pub2" || exit 1
fi
# A key was asked for, so the tool that checks it must be there: found out
# now, not after two downloads.
if [ -n "$pub" ] && ! command -v minisign >/dev/null 2>&1; then
	echo "install.sh: CADENCE_MINISIGN_PUB is set but minisign is not installed" >&2
	exit 1
fi
if [ "$upgrade_only" = "true" ]; then
	if [ -z "$pub" ]; then
		echo "install.sh: CADENCE_UPGRADE_ONLY requires CADENCE_MINISIGN_PUB: the downgrade check executes the downloaded binary, which is only safe once its signature is verified" >&2
		exit 1
	fi
	if ! command -v timeout >/dev/null 2>&1; then
		echo "install.sh: CADENCE_UPGRADE_ONLY requires the timeout command (coreutils)" >&2
		exit 1
	fi
fi

temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT HUP INT TERM

# The floor: the highest version the host already runs. Read before the first
# download, so a host that cannot establish it never fetches anything.
if [ "$upgrade_only" = "true" ]; then
	read_floor "$canon_bin"
	read_floor "$legacy_bin"
	if [ "$floor_found" -eq 0 ]; then
		echo "install.sh: CADENCE_UPGRADE_ONLY found no agent binary at $canon_bin or $legacy_bin: refusing, there is no running version to compare with. Reinstall without CADENCE_UPGRADE_ONLY, with a new enrollment code" >&2
		exit 1
	fi
	echo "install.sh: running agent version $floor_maj.$floor_min.$floor_pat (downgrade floor)"
fi

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

if [ -n "$pub" ]; then
	fetch agent/cadence-agent.minisig "$temporary/cadence-agent.minisig"
	# PUB first; PUB2 only when it is set and the first verification failed.
	if try_key "$pub"; then
		signer=$pub
	elif [ -n "$pub2" ] && try_key "$pub2"; then
		signer=$pub2
	else
		signer=""
	fi
	if [ -z "$signer" ]; then
		tried=$(key_id "$(pub_key "$pub")")
		if [ -n "$pub2" ]; then tried="$tried, $(key_id "$(pub_key "$pub2")")"; fi
		claimed=$(key_id "$(sed -n 2p "$temporary/cadence-agent.minisig" 2>/dev/null || true)")
		echo "install.sh: minisign signature invalid (tried keys: $tried; signature claims key $claimed, unverified)" >&2
		exit 1
	fi
	echo "install.sh: minisign signature OK (key $(key_id "$(pub_key "$signer")"))"
	if [ "$upgrade_only" = "true" ]; then
		# FIRST EXECUTION POINT of the downloaded binary: sha256 and signature
		# are both verified, and nothing on the host has changed yet.
		chmod 0700 "$temporary/cadence-agent"
		if ! probe_version "$temporary/cadence-agent"; then
			echo "install.sh: the downloaded agent cannot report its version ($probe_err): refusing" >&2
			exit 1
		fi
		if ! version_triple "$probe_text"; then
			echo "install.sh: the downloaded agent does not report a strict N.N.N version: refusing" >&2
			exit 1
		fi
		if triple_gt "$floor_maj" "$floor_min" "$floor_pat" "$v_maj" "$v_min" "$v_pat"; then
			echo "install.sh: refusing to downgrade: the host runs $floor_maj.$floor_min.$floor_pat, the server offers $v_maj.$v_min.$v_pat" >&2
			exit 1
		fi
		echo "install.sh: downgrade check OK (host $floor_maj.$floor_min.$floor_pat, server $v_maj.$v_min.$v_pat)"
	fi
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
if [ "$verified_ca" = "$server_ca" ]; then
	echo "install.sh: verified CA is already the installed server CA"
else
	install -m 0644 "$verified_ca" "$server_ca"
fi
update-ca-certificates >/dev/null 2>&1
install -m 0755 "$temporary/cadence-agent" /usr/bin/cadence-agent
for unit in cadence-agent.service cadence-agent.timer \
	cadence-agent-poll.service cadence-agent-poll.timer \
	cadence-agent-health-check-boot.service cadence-agent-health-check-boot.timer; do
	install -m 0644 "$temporary/$unit" "/etc/systemd/system/$unit"
done

# The code arrives on stdin, never argv or the environment. The binary creates
# the private key locally and switches agent.env only after all credentials
# have been validated and written. Authenticated upgrades preserve the
# existing credential bundle and need no new enrollment code.
if [ "$upgrade_only" = "false" ]; then
	/usr/bin/cadence-agent -enroll -enroll-server "$base" \
		-enroll-ca "$server_ca" -enroll-directory /etc/cadence
else
	echo "install.sh: preserving enrolled credentials"
fi
echo "install.sh: agent $(/usr/bin/cadence-agent -version 2>/dev/null || echo '(installed)')"

systemctl daemon-reload
systemctl enable --now cadence-agent.timer cadence-agent-poll.timer \
	cadence-agent-health-check-boot.timer >/dev/null
echo "install.sh: timers enabled. First report:"
systemctl start cadence-agent.service || true
echo "  journalctl -u cadence-agent -n 20 --no-pager"
