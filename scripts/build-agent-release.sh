#!/bin/sh
# BREAK-GLASS: build, smoke-test, hash and sign an agent release bundle by
# hand with the offline backup key.
#
# The normal release path is CI: an agent-v* tag makes
# .github/workflows/release.yml build, sign (active key, GitHub environment
# "release") and publish a draft Release, which the server then fetches with
# scripts/publish-agent.sh fetch. Use this script only when that path is
# unavailable or distrusted: the active key is lost or compromised, or GitHub
# cannot be used. It signs with the key you name, on the machine you run it
# on, and never needs the Cadence server or any server secret.
#
#   CADENCE_SIGNING_KEY=<secret key file> scripts/build-agent-release.sh <version> <outdir>
#
# Needs a Cadence checkout, docker and minisign, on linux/amd64 (the
# -version smoke executes the amd64 binary). The secret key is typically the
# password-protected backup key (agent/minisign-backup.pub): minisign asks for
# its password at the terminal, once per architecture. Restore it to a
# temporary location with scripts/restore-signing-key.sh and delete it after
# use. There is no default key path on purpose.
#
# Produces a self-contained transfer bundle (amd64 + arm64 triplets plus a
# version file, nothing else):
#
#   <outdir>/cadence-agent-linux-amd64{,.sha256,.minisig}
#   <outdir>/cadence-agent-linux-arm64{,.sha256,.minisig}
#   <outdir>/version
#
# Signatures carry the same trusted comment as the CI ones
# ("cadence-agent <version> linux/<arch> sha256:<hash>", one definition in
# scripts/agent-sign-lib.sh) and are verified against the whole trust set,
# agent/minisign.pub then agent/minisign-backup.pub: the log names the key that
# verified, and the build fails if neither does.
#
# Release discipline, enforced fail-closed (scripts/check-release-source.sh):
# check out the release source explicitly, then build from it:
#
#   git fetch --tags origin
#   git checkout agent-v<version>   # must match agent/CHANGELOG.md "## <version>"
#   CADENCE_SIGNING_KEY=<key> scripts/build-agent-release.sh <version> /tmp/agent-<version>
#   # transfer /tmp/agent-<version> to the server, then there:
#   scripts/publish-agent.sh publish <version> <transferred-dir>
#
# Anything else refuses: missing tag, HEAD anywhere but the tag's commit,
# tracked or staged edits, untracked files under agent/. The signature step
# signs the bytes just built here: a compromised Cadence server can neither
# influence these bytes nor obtain a signature for anything else.
#
# Environment:
#   CADENCE_SIGNING_KEY   secret key to sign with (required, no default)
#   CADENCE_RELEASE_REPO  checkout to build from (default: this repo;
#                         the test suite points it at temp repos)
#   CADENCE_PUBLISH_PUBKEY, CADENCE_PUBLISH_PUBKEY_BACKUP
#                         trust set paths, tests only (the variables
#                         publish-agent.sh reads); a warning is printed when
#                         either is set.
# (CADENCE_RELEASE_PUBKEY, which let the self-check use any key, is gone.)

set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=${CADENCE_RELEASE_REPO:-$(CDPATH='' cd -- "$here/.." && pwd)}
cd "$repo"

# shellcheck source=scripts/agent-sign-lib.sh
. "$here/agent-sign-lib.sh"

key=${CADENCE_SIGNING_KEY:-}
ARCHS="amd64 arm64"

if [ $# -ne 2 ]; then
	echo "usage: CADENCE_SIGNING_KEY=<secret key file> build-agent-release.sh <version> <outdir>" >&2
	exit 2
fi
ver=$1
out=$2

if [ -n "${CADENCE_RELEASE_PUBKEY:-}" ]; then
	echo "build-agent-release.sh: CADENCE_RELEASE_PUBKEY no longer exists: the signature is verified against the committed trust set" >&2
	exit 2
fi
if [ -z "$key" ]; then
	echo "build-agent-release.sh: CADENCE_SIGNING_KEY is required (break-glass tool: no default key; see the header)" >&2
	exit 2
fi

if [ "$(uname -s)" != "Linux" ] || [ "$(uname -m)" != "x86_64" ]; then
	echo "build-agent-release.sh: release builds run on linux/amd64 only" >&2
	exit 1
fi
if ! sh "$here/check-release-source.sh" "$ver"; then
	exit 1
fi
if [ ! -f "$key" ]; then
	echo "build-agent-release.sh: no signing key at $key" >&2
	exit 1
fi
trust_set_load "$repo"
if ! trust_set_check; then
	exit 1
fi
if [ -e "$out" ] && [ -n "$(ls -A "$out")" ]; then
	echo "build-agent-release.sh: refusing non-empty outdir $out" >&2
	exit 1
fi

stage="$out.tmp.$$"
rm -rf "$stage"
mkdir -p "$stage"
# A signal trap that only cleans up lets the build carry on afterwards: the
# signal traps exit (128 + signal) and the EXIT trap does the cleaning.
trap 'rm -rf "$stage"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "build-agent-release.sh: building agent $ver (linux/amd64+arm64, static)"
for arch in $ARCHS; do
	docker run --rm -e "AGENT_VERSION=$ver" -v "$repo/agent":/s -w /s golang:1.27 \
		sh -c 'set -e; arch=$1
			CGO_ENABLED=0 GOOS=linux GOARCH="$arch" go build -trimpath \
				-ldflags "-X main.agentVersion=$AGENT_VERSION" \
				-o bin/cadence-agent ./cmd/agent' sh "$arch"
	cp "$repo/agent/bin/cadence-agent" "$stage/cadence-agent-linux-$arch"
done

if cmp -s "$stage/cadence-agent-linux-amd64" "$stage/cadence-agent-linux-arm64"; then
	echo "build-agent-release.sh: amd64 and arm64 binaries are identical" >&2
	exit 1
fi
for arch in $ARCHS; do
	if ! docker run --rm -v "$stage":/b golang:1.27 \
		sh -c 'go version -m "/b/cadence-agent-linux-$1" | grep -q "GOARCH=$1"' sh "$arch"; then
		echo "build-agent-release.sh: cadence-agent-linux-$arch is not $arch" >&2
		exit 1
	fi
done
got=$("$stage/cadence-agent-linux-amd64" -version 2>/dev/null || echo '?')
if [ "$got" != "$ver" ]; then
	echo "build-agent-release.sh: built binary reports '$got', want '$ver'" >&2
	exit 1
fi

(
	cd "$stage"
	for arch in $ARCHS; do
		sha256sum "cadence-agent-linux-$arch" >"cadence-agent-linux-$arch.sha256"
	done
	printf '%s\n' "$ver" >version
)

echo "build-agent-release.sh: signing with $key (the key's password is asked by minisign, once per architecture)"
if ! sign_bundle "$key" "$ver" "$stage"; then
	echo "build-agent-release.sh: signing or self-check failed" >&2
	exit 1
fi

mv "$stage" "$out"
trap - EXIT INT TERM

echo "build-agent-release.sh: bundle ready at $out"
sha256sum "$out/cadence-agent-linux-amd64" "$out/cadence-agent-linux-arm64" | sed 's/^/  /'
echo "  transfer it to the server, then there:"
echo "    scripts/publish-agent.sh publish $ver <transferred-dir>"
