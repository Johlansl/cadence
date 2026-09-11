#!/bin/sh
# Create a short-lived enrollment code through the admin API.
#
# New host:
#   scripts/provision-host.sh <hostname> [description]
# Existing host migration:
#   scripts/provision-host.sh --host-id <uuid> <hostname>

set -eu

target_host_id=""
if [ "${1:-}" = "--host-id" ]; then
	target_host_id=${2:-}
	hostname=${3:-}
	description=""
else
	hostname=${1:-}
	description=${2:-}
fi
if [ -z "$hostname" ] || { [ "${1:-}" = "--host-id" ] && [ -z "$target_host_id" ]; }; then
	echo "usage: $0 <hostname> [description]" >&2
	echo "       $0 --host-id <uuid> <hostname>" >&2
	exit 2
fi

api=${CADENCE_API:-http://127.0.0.1:8000}
dashboard_url=${CADENCE_DASHBOARD_URL:-https://cadence.lan}
ttl=${CADENCE_ENROLLMENT_TTL_MINUTES:-30}

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
env_file=${CADENCE_ENV_FILE:-$here/../.env}
admin_key=${CADENCE_ADMIN_KEY:-}
if [ -z "$admin_key" ] && [ -f "$env_file" ]; then
	admin_key=$(sed -n 's/^CADENCE_ADMIN_KEY=//p' "$env_file" | head -n 1)
fi
if [ -z "$admin_key" ]; then
	echo "provision-host.sh: no admin key (set CADENCE_ADMIN_KEY or CADENCE_ENV_FILE)" >&2
	exit 1
fi

case "$hostname$description$target_host_id$ttl" in
*'"'*) echo "provision-host.sh: quotes are not allowed in arguments" >&2; exit 2 ;;
esac
case "$ttl" in
''|*[!0-9]*) echo "provision-host.sh: TTL must be an integer from 5 to 240" >&2; exit 2 ;;
esac
if [ "$ttl" -lt 5 ] || [ "$ttl" -gt 240 ]; then
	echo "provision-host.sh: TTL must be from 5 to 240 minutes" >&2
	exit 2
fi

if [ -n "$target_host_id" ]; then
	body=$(printf '{"target_host_id":"%s","ttl_minutes":%s}' "$target_host_id" "$ttl")
elif [ -n "$description" ]; then
	body=$(printf '{"expected_hostname":"%s","description":"%s","ttl_minutes":%s}' \
		"$hostname" "$description" "$ttl")
else
	body=$(printf '{"expected_hostname":"%s","ttl_minutes":%s}' "$hostname" "$ttl")
fi

response=$(
	curl -sS -X POST "$api/api/v1/admin/enrollments" \
		-H "X-Admin-Key: $admin_key" \
		-H 'Content-Type: application/json' \
		-d "$body" \
		-w '\n%{http_code}'
)
status_code=$(printf '%s\n' "$response" | tail -n 1)
payload=$(printf '%s\n' "$response" | sed '$d')
if [ "$status_code" != "201" ]; then
	echo "provision-host.sh: API returned $status_code" >&2
	printf '%s\n' "$payload" >&2
	exit 1
fi

id=$(printf '%s' "$payload" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')
code=$(printf '%s' "$payload" | sed -n 's/.*"code":"\([^"]*\)".*/\1/p')
expires_at=$(printf '%s' "$payload" | sed -n 's/.*"expires_at":"\([^"]*\)".*/\1/p')
if [ -z "$id" ] || [ -z "$code" ] || [ -z "$expires_at" ]; then
	echo "provision-host.sh: could not parse the API response" >&2
	exit 1
fi

cat <<EOF
Enrollment created for $hostname (id $id, expires $expires_at).

Enrollment code (shown once; copy it manually to the target host):

  $code

Securely copy scripts/agent-bootstrap.sh from this checkout to the target
(for example with scp over SSH). On the target, run:

  sudo CADENCE_DASHBOARD_URL=$dashboard_url ./agent-bootstrap.sh

The bootstrap prompts for the code. It downloads only /agent/ca.crt over HTTP,
verifies its exact SHA-256 fingerprint before trusting it, and obtains the
installer and every remaining asset over authenticated HTTPS.
EOF
