#!/bin/sh
# Build, smoke-test, hash and sign an agent release bundle.
#
# Run on the KEY-HOLDER machine (operator laptop/vault) -- never on the
# Cadence server. This machine needs a Cadence checkout, docker and
# minisign. It needs no access to the server and no server secrets.
#
#   scripts/build-agent-release.sh <version> <outdir>
#
# Produces a self-contained transfer bundle (amd64 + arm64 triplets plus a
# version file, nothing else):
#
#   <outdir>/cadence-agent-linux-amd64{,.sha256,.minisig}
#   <outdir>/cadence-agent-linux-arm64{,.sha256,.minisig}
#   <outdir>/version
#
# Release discipline, enforced fail-closed (scripts/check-release-source.sh):
# check out the release source explicitly, then build from it:
#
#   git fetch --tags origin
#   git checkout agent-v<version>   # must match agent/CHANGELOG.md "## <version>"
#   scripts/build-agent-release.sh <version> /tmp/agent-<version>
#   # transfer /tmp/agent-<version> to the server, then there:
#   scripts/publish-agent.sh publish <version> <transferred-dir>
#
# Anything else refuses: missing tag, HEAD anywhere but the tag's commit,
# tracked or staged edits, untracked files under agent/. The signature step
# signs the bytes just built here, on this machine: a compromised Cadence
# server can neither influence these bytes nor obtain a signature for
# anything else. Release builds run on linux/amd64 only (the -version
# smoke executes the amd64 binary).
#
# Environment:
#   CADENCE_SIGNING_KEY   private key to sign with
#                         (default: ~/.cadence/minisign.key)
#   CADENCE_RELEASE_PUBKEY   public key for the self-check (default:
#                         <repo>/agent/minisign.pub; override in tests only)
#   CADENCE_RELEASE_REPO  checkout to build from (default: this repo;
#                         the test suite points it at temp repos)

set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=${CADENCE_RELEASE_REPO:-$(CDPATH='' cd -- "$here/.." && pwd)}
cd "$repo"

key=${CADENCE_SIGNING_KEY:-$HOME/.cadence/minisign.key}
pub=${CADENCE_RELEASE_PUBKEY:-$repo/agent/minisign.pub}
ARCHS="amd64 arm64"

if [ $# -ne 2 ]; then
	echo "usage: build-agent-release.sh <version> <outdir>" >&2
	exit 2
fi
ver=$1
out=$2

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
if [ ! -f "$pub" ]; then
	echo "build-agent-release.sh: no public key at $pub" >&2
	exit 1
fi
if [ -e "$out" ] && [ -n "$(ls -A "$out")" ]; then
	echo "build-agent-release.sh: refusing non-empty outdir $out" >&2
	exit 1
fi

stage="$out.tmp.$$"
rm -rf "$stage"
mkdir -p "$stage"
trap 'rm -rf "$stage"' EXIT INT TERM

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

echo "build-agent-release.sh: signing (trusted comment: cadence-agent $ver)"
for arch in $ARCHS; do
	minisign -S -s "$key" -m "$stage/cadence-agent-linux-$arch" \
		-t "cadence-agent $ver"
done

# Self-check against the same public key the server will verify with.
for arch in $ARCHS; do
	if ! minisign -V -p "$pub" -m "$stage/cadence-agent-linux-$arch" \
		-x "$stage/cadence-agent-linux-$arch.minisig" >/dev/null 2>&1; then
		echo "build-agent-release.sh: self-check failed for $arch" >&2
		exit 1
	fi
done

mv "$stage" "$out"
trap - EXIT INT TERM

echo "build-agent-release.sh: bundle ready at $out"
sha256sum "$out/cadence-agent-linux-amd64" "$out/cadence-agent-linux-arm64" | sed 's/^/  /'
echo "  transfer it to the server, then there:"
echo "    scripts/publish-agent.sh publish $ver <transferred-dir>"
