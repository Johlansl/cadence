#!/bin/sh
# Build the agent and stage everything the one-liner installer serves.
#
# Run on the central server, from a checkout of this repo, with the stack up
# (the CA is read from the running caddy container).
#
#   scripts/publish-agent.sh
#
# Populates ./dist (mounted read-only into caddy at /srv/dist), which Caddy
# serves over plain HTTP at:
#   /install.sh
#   /agent/cadence-agent   /agent/cadence-agent.sha256
#   /agent/ca.crt
#   /agent/systemd/<unit>

set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/.." && pwd)
cd "$repo"

dist="$repo/dist"
mkdir -p "$dist/agent/systemd"

echo "publish-agent.sh: building the agent (linux/amd64, static)"
docker run --rm -v "$repo/agent":/s -w /s golang:1.23 sh -c \
	'CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o bin/cadence-agent ./cmd/agent'

install -m 0755 "$repo/agent/bin/cadence-agent" "$dist/agent/cadence-agent"
( cd "$dist/agent" && sha256sum cadence-agent >cadence-agent.sha256 )

install -m 0644 "$repo/scripts/agent-install.sh" "$dist/install.sh"
for unit in cadence-agent.service cadence-agent.timer \
	cadence-agent-poll.service cadence-agent-poll.timer; do
	install -m 0644 "$repo/agent/systemd/$unit" "$dist/agent/systemd/$unit"
done

caddy_cid=$(docker compose ps -q caddy)
if [ -z "$caddy_cid" ]; then
	echo "publish-agent.sh: caddy not running -- cannot export the CA" >&2
	exit 1
fi
docker compose exec -T caddy \
	cat /data/caddy/pki/authorities/local/root.crt >"$dist/agent/ca.crt"

version=$("$dist/agent/cadence-agent" -version 2>/dev/null || echo '?')
echo "publish-agent.sh: staged agent $version + CA + units in $dist"
echo "  test: curl -fsSL http://\${CADENCE_SITE_ADDRESS:-cadence.lan}/install.sh | head"
