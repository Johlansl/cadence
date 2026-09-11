#!/bin/sh
# Build the agent and stage everything the authenticated installer fetches.
#
# Run on the central server, from a checkout of this repo, with the stack up
# (the CA is read from the running caddy container).
#
#   scripts/publish-agent.sh
#
# Populates ./dist (mounted read-only into caddy at /srv/dist). Caddy serves
# only /agent/ca.crt over HTTP; the installer and all other paths require HTTPS.
# Staged paths:
#   /install.sh
#   /agent/cadence-agent   /agent/cadence-agent.sha256
#   /agent/cadence-agent.minisig   (only if a signing key is configured)
#   /agent/ca.crt
#   /agent/systemd/<unit>
#
# Signing (optional): with `minisign` installed and a secret key at
# CADENCE_MINISIGN_KEY (default ~/.cadence/minisign.key), the binary is signed
# and cadence-agent.minisig is published. Generate a passwordless key once with
#   minisign -G -W -p agent/minisign.pub -s ~/.cadence/minisign.key
# then commit agent/minisign.pub and hand it to each host out of band. Without
# a key, publishing continues unsigned (hosts fall back to the sha256 check).

set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH='' cd -- "$here/.." && pwd)
cd "$repo"

dist="$repo/dist"
mkdir -p "$dist/agent/systemd"

# Version = the newest `agent-v*` tag reachable from HEAD (git describe), with
# the `agent-v` prefix stripped; empty when there is no such tag. Computed on
# the host -- the build container only mounts agent/, not .git.
agent_version=$(git -C "$repo" describe --tags --match 'agent-v*' --dirty 2>/dev/null \
	| sed 's/^agent-v//' || true)

echo "publish-agent.sh: building the agent (linux/amd64, static)${agent_version:+, version $agent_version}"
docker run --rm -e "AGENT_VERSION=$agent_version" -v "$repo/agent":/s -w /s golang:1.23 sh -c '
	set -e
	ldflags=""
	[ -n "$AGENT_VERSION" ] && ldflags="-X main.agentVersion=$AGENT_VERSION"
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath \
		${ldflags:+-ldflags "$ldflags"} -o bin/cadence-agent ./cmd/agent
'

install -m 0755 "$repo/agent/bin/cadence-agent" "$dist/agent/cadence-agent"
( cd "$dist/agent" && sha256sum cadence-agent >cadence-agent.sha256 )

version=$("$dist/agent/cadence-agent" -version 2>/dev/null || echo '?')

# Sign the binary with minisign. If agent/minisign.pub is committed, signing is
# expected and a missing key is a hard error (don't silently ship unsigned).
# With no agent/minisign.pub in the repo, publish unsigned (sha256 only).
#
# Forking this repo to run your own Cadence? The committed agent/minisign.pub is
# the upstream key and you cannot have its private half. Replace it with your
# own (minisign -G -W -p agent/minisign.pub -s ~/.cadence/minisign.key), or
# delete it to publish unsigned.
minisign_key=${CADENCE_MINISIGN_KEY:-$HOME/.cadence/minisign.key}
if command -v minisign >/dev/null 2>&1 && [ -f "$minisign_key" ]; then
	minisign -S -s "$minisign_key" \
		-m "$dist/agent/cadence-agent" \
		-x "$dist/agent/cadence-agent.minisig" \
		-t "cadence-agent $version"
	echo "publish-agent.sh: signed cadence-agent.minisig"
	if [ -f "$repo/agent/minisign.pub" ]; then
		echo "publish-agent.sh: hand this public key to every host OUT OF BAND:"
		sed 's/^/    /' "$repo/agent/minisign.pub"
	fi
elif [ -f "$repo/agent/minisign.pub" ]; then
	echo "publish-agent.sh: agent/minisign.pub is committed but there is no" \
		"signing key at $minisign_key (or minisign is not installed) --" \
		"refusing to publish an unsigned release" >&2
	exit 1
else
	rm -f "$dist/agent/cadence-agent.minisig"
	echo "publish-agent.sh: NOT signed (no agent/minisign.pub in the repo)" \
		"-- hosts will use the sha256 check only"
fi

install -m 0644 "$repo/scripts/agent-install.sh" "$dist/install.sh"
for unit in cadence-agent.service cadence-agent.timer \
	cadence-agent-poll.service cadence-agent-poll.timer \
	cadence-agent-health-check-boot.service cadence-agent-health-check-boot.timer; do
	install -m 0644 "$repo/agent/systemd/$unit" "$dist/agent/systemd/$unit"
done

caddy_cid=$(docker compose ps -q caddy)
if [ -z "$caddy_cid" ]; then
	echo "publish-agent.sh: caddy not running -- cannot export the CA" >&2
	exit 1
fi
docker compose exec -T caddy \
	cat /data/caddy/pki/authorities/local/root.crt >"$dist/agent/ca.crt"

echo "publish-agent.sh: staged agent $version + CA + units in $dist"
echo "  CA bootstrap: http://\${CADENCE_SITE_ADDRESS:-cadence.lan}/agent/ca.crt"
echo "  remaining assets: https://\${CADENCE_SITE_ADDRESS:-cadence.lan}/"
