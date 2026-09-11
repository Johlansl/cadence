#!/bin/sh
# Trusted first-contact prelude for a Cadence host. Copy this file to the host
# through an authenticated channel (for example scp from the server checkout),
# then run it as root. Never download this script over plain HTTP.

set -eu
set -f

dashboard_url=${CADENCE_DASHBOARD_URL:-https://cadence.lan}
case "$dashboard_url" in
https://*) ;;
*) echo "agent-bootstrap.sh: CADENCE_DASHBOARD_URL must use https" >&2; exit 2 ;;
esac

if [ "$(id -u)" != "0" ]; then
	echo "agent-bootstrap.sh: run as root (sudo)" >&2
	exit 1
fi
for command in curl sha256sum mktemp; do
	if ! command -v "$command" >/dev/null 2>&1; then
		echo "agent-bootstrap.sh: $command not found on PATH" >&2
		exit 1
	fi
done

printf 'Enrollment code: ' >&2
if ! IFS= read -r code </dev/tty 2>/dev/null; then
	IFS= read -r code
fi

old_ifs=$IFS
IFS=.
set -- $code
IFS=$old_ifs
if [ "$#" -ne 3 ] || [ "$1" != "cad1" ] || [ "${#2}" -ne 32 ] || [ "${#3}" -ne 64 ]; then
	echo "agent-bootstrap.sh: invalid enrollment code format" >&2
	exit 1
fi
expected_fingerprint=$3
case "$expected_fingerprint" in
*[!0-9a-f]*) echo "agent-bootstrap.sh: invalid CA fingerprint" >&2; exit 1 ;;
esac

authority=${dashboard_url#https://}
authority=${authority%%/*}
host=${authority%%:*}
if [ -z "$host" ]; then
	echo "agent-bootstrap.sh: cannot derive the dashboard host" >&2
	exit 2
fi
ca_url="http://$host/agent/ca.crt"

temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT HUP INT TERM
ca_file=$temporary/server-ca.crt
installer=$temporary/install.sh
code_file=$temporary/enrollment-code
umask 077
printf '%s\n' "$code" >"$code_file"
unset code

# This is the only unauthenticated download. Its exact bytes are authenticated
# by the fingerprint carried inside the manually transferred enrollment code.
curl -fsSL --proto '=http' --proto-redir '=http' "$ca_url" -o "$ca_file"
actual_fingerprint=$(sha256sum "$ca_file" | awk '{print $1}')
if [ "$actual_fingerprint" != "$expected_fingerprint" ]; then
	echo "agent-bootstrap.sh: server CA fingerprint mismatch; refusing enrollment" >&2
	echo "  expected: $expected_fingerprint" >&2
	echo "  received: $actual_fingerprint" >&2
	exit 1
fi
echo "agent-bootstrap.sh: server CA fingerprint verified"

# Every executable byte and every credential exchange after the pin check uses
# HTTPS rooted exclusively in the verified CA.
curl -fsSL --proto '=https' --proto-redir '=https' --tlsv1.2 \
	--cacert "$ca_file" "$dashboard_url/install.sh" -o "$installer"
chmod 0700 "$installer"
CADENCE_BASE_URL=$dashboard_url CADENCE_VERIFIED_CA=$ca_file \
	sh "$installer" <"$code_file"
