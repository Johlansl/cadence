#!/bin/sh
# Provision a monitored host and print its agent config.
#
# Run on the central server (talks to the API over loopback by default).
#
#   scripts/provision-host.sh <hostname> [description]
#
# Environment:
#   CADENCE_API         API base URL to provision against
#                       (default: http://127.0.0.1:8000)
#   CADENCE_ADMIN_KEY   admin key; if unset, read from CADENCE_ENV_FILE
#   CADENCE_ENV_FILE    .env to read CADENCE_ADMIN_KEY from
#                       (default: <repo>/.env)
#   CADENCE_AGENT_URL   value written as CADENCE_SERVER_URL in the printed
#                       agent.env block (default: https://cadence.lan)

set -eu

hostname=${1:-}
description=${2:-}
if [ -z "$hostname" ]; then
	echo "usage: $0 <hostname> [description]" >&2
	exit 2
fi

api=${CADENCE_API:-http://127.0.0.1:8000}
agent_url=${CADENCE_AGENT_URL:-https://cadence.lan}

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

# Build the JSON body without assuming jq. hostname/description are simple
# identifiers here; reject anything with a double quote to stay safe.
case "$hostname$description" in
*'"'*) echo "provision-host.sh: quotes are not allowed in the arguments" >&2; exit 2 ;;
esac
if [ -n "$description" ]; then
	body=$(printf '{"hostname":"%s","description":"%s"}' "$hostname" "$description")
else
	body=$(printf '{"hostname":"%s"}' "$hostname")
fi

response=$(
	curl -sS -X POST "$api/api/v1/admin/hosts" \
		-H "X-Admin-Key: $admin_key" \
		-H 'Content-Type: application/json' \
		-d "$body" \
		-w '\n%{http_code}'
)
code=$(printf '%s\n' "$response" | tail -n 1)
payload=$(printf '%s\n' "$response" | sed '$d')

if [ "$code" != "201" ]; then
	echo "provision-host.sh: API returned $code" >&2
	printf '%s\n' "$payload" >&2
	exit 1
fi

# token is url-safe base64 (secrets.token_urlsafe): [A-Za-z0-9_-], no quotes.
id=$(printf '%s' "$payload" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')
token=$(printf '%s' "$payload" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
if [ -z "$id" ] || [ -z "$token" ]; then
	echo "provision-host.sh: could not parse the response:" >&2
	printf '%s\n' "$payload" >&2
	exit 1
fi

install_host=$(printf '%s\n' "$agent_url" | sed -E 's#^[a-z]+://##; s#/.*##')

cat <<EOF
host "$hostname" created (id $id)

One-liner -- run on $hostname as root (needs scripts/publish-agent.sh to have
been run once on the server):

  curl -fsSL http://$install_host/install.sh | sudo CADENCE_TOKEN=$token sh

Or by hand -- paste into /etc/cadence/agent.env (chmod 0600, root):

  CADENCE_SERVER_URL=$agent_url
  CADENCE_TOKEN=$token

then trust the CA, install update-notifier-common, drop the agent binary +
units, and enable the timers (README "Install an agent").

This token is returned only once by the API. It is not retrievable through the
API, but an operator with database access and CADENCE_TOKEN_ENCRYPTION_KEY can
recover its encrypted copy.
EOF
