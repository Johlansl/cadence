#!/bin/sh
# Verify and stage versioned agent release artifacts.
#
# The agent binary is served to hosts from ./dist (mounted read-only into
# caddy at /srv/dist). A published version is an immutable directory:
#
#   dist/agent/v<version>/cadence-agent-linux-amd64{,.sha256,.minisig}
#   dist/agent/v<version>/cadence-agent-linux-arm64{,.sha256,.minisig}
#
# plus the unversioned install triplet (amd64 of the latest published
# version, reshaped for the existing installer, which fetches fixed names):
#
#   dist/agent/cadence-agent{,.sha256,.minisig}
#   dist/install.sh  dist/agent/ca.crt  dist/agent/systemd/<unit>
#
# Threat-model note (why this script never builds): an earlier revision had
# the server build the binaries and the operator sign the server's bytes.
# That defeats offline signing: a root-compromised server builds a malicious
# binary, the operator signs it blindly, and server-side signature checks
# then "verify" the attacker's bytes. Signature verification only proves the
# staged bytes are the signed bytes; it says nothing about a build the
# attacker controlled. So the server is never the source of signed bytes:
# releases are built AND signed on the key-holder machine
# (scripts/build-agent-release.sh), and this script only verifies and stages
# the transferred bundle.
#
# Publishing is an explicit two-machine flow (never part of
# scripts/deploy.sh):
#
#   1. key-holder: scripts/build-agent-release.sh <version> <outdir>
#      Builds both binaries from an identified checkout, smoke-tests them,
#      hashes and signs them. See that script's header for the release
#      discipline (tag/version/commit binding, air-gap transfer).
#   2. server:     scripts/publish-agent.sh publish <version> <indir>
#      Verifies (grammar, version binding, closed file set, triplet
#      completeness per arch, recomputed SHA-256, minisign -V against the
#      committed agent/minisign.pub, amd64 -version AFTER the signature
#      verifies, ELF machine per arch) then stages atomically: the version
#      directory appears via one rename, and the unversioned triplet is
#      refreshed from the just-published amd64 files. Refuses loudly if
#      the version already exists: published versions are immutable, a bad
#      release needs a new version, never a republish.
#
#   scripts/publish-agent.sh assets
#      Stage only the unversioned bootstrap assets (installer, units, CA).
#      This is what scripts/deploy.sh runs: backend/frontend deploys never
#      need an agent release or a signature.
#
# Rules, enforced by scripts/test-publish-agent.sh:
# - this script never builds (no compiler, no docker for binaries), never
#   signs (no `minisign -S`) and never reads a private key; it only
#   verifies with the committed public key, using sh + coreutils +
#   minisign (no Go toolchain needed);
# - downloaded code never runs before its signature verifies: the amd64
#   -version smoke runs only after minisign -V succeeds, on a linux/amd64
#   publish host (the server target is amd64); anything else refuses;
# - unsigned publishing does not exist: no agent/minisign.pub, no publish
#   (a fork must generate its own key, it cannot publish unsigned);
# - absent version/arch/triplet member, or any unexpected extra file in
#   the bundle: the version is not published, and Caddy answers 404 for it
#   (no fallback);
# - the private key must never be copied onto the Cadence server, not even
#   temporarily. Losing the key means rotation (docs/decisions.md), never
#   disabling signatures.
#
# Environment (advanced/test only):
#   CADENCE_PUBLISH_DIST     dist root to stage into (default: <repo>/dist)
#   CADENCE_PUBLISH_PUBKEY   minisign public key to verify with
#                            (default: <repo>/agent/minisign.pub)

set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH='' cd -- "$here/.." && pwd)
cd "$repo"

dist=${CADENCE_PUBLISH_DIST:-$repo/dist}
pubkey=${CADENCE_PUBLISH_PUBKEY:-$repo/agent/minisign.pub}

ARCHS="amd64 arm64"
UNITS="cadence-agent.service cadence-agent.timer
cadence-agent-poll.service cadence-agent-poll.timer
cadence-agent-health-check-boot.service cadence-agent-health-check-boot.timer"

usage() {
	echo "usage: publish-agent.sh publish <version> <indir>   (verify + atomic stage)" >&2
	echo "       publish-agent.sh assets                      (bootstrap assets only)" >&2
	exit 2
}

# Strict N.N.N grammar for publishable versions (leading zeros accepted,
# suffixes refused).
check_version() {
	case $1 in
	*[!0-9.]*|""|.*|*.|*..*) return 1 ;;
	esac
	_v1=${1%%.*}; _rest=${1#*.}
	case $_rest in *.*) ;; *) return 1 ;; esac
	_v2=${_rest%%.*}; _v3=${_rest#*.}
	case $_v3 in *.*) return 1 ;; esac
	[ -n "$_v1" ] && [ -n "$_v2" ] && [ -n "$_v3" ] || return 1
	# Canonical decimal only (6B): one release, one spelling. A component
	# is "0" or starts with 1-9; "01.2.3" refuses like the agent does.
	for _v in "$_v1" "$_v2" "$_v3"; do
		case $_v in 0*) [ "$_v" = "0" ] || return 1 ;; esac
	done
	return 0
}

# Print the ELF e_machine of a file (amd64 = 62, arm64 = 183), or fail.
# Needs only od: no Go toolchain, no docker, no `file`.
elf_machine() {
	magic=$(od -A n -t x1 -N 4 "$1" 2>/dev/null | tr -d ' \n') || return 1
	[ "$magic" = "7f454c46" ] || return 1
	od -A n -t u2 -j 18 -N 2 "$1" 2>/dev/null | tr -d ' \n'
}

# Verify one (binary, sha256, minisig) triplet. The .sha256 file is parsed,
# never trusted as a checksum root: only its hash field is compared
# against a locally recomputed digest. Order matters: nothing runs before
# the signature verifies.
verify_triplet() {
	arch=$1
	indir=$2
	ver=$3
	bin="$indir/cadence-agent-linux-$arch"
	sha="$bin.sha256"
	sig="$bin.minisig"
	for f in "$bin" "$sha" "$sig"; do
		if [ ! -f "$f" ]; then
			echo "publish-agent.sh: missing $f" >&2
			return 1
		fi
	done
	want=$(awk '{print $1}' "$sha")
	case $want in
	*[!0-9a-f]*|"") echo "publish-agent.sh: $sha is not hex" >&2; return 1 ;;
	esac
	if [ "${#want}" -ne 64 ]; then
		echo "publish-agent.sh: $sha is not a SHA-256" >&2
		return 1
	fi
	got=$(sha256sum "$bin" | awk '{print $1}')
	if [ "$want" != "$got" ]; then
		echo "publish-agent.sh: checksum mismatch for $bin" >&2
		echo "  file says $want" >&2
		echo "  actual   $got" >&2
		return 1
	fi
	# minisign -V checks the message signature AND the trusted comment's
	# global signature. No fallback if minisign is missing.
	if ! minisign -V -p "$pubkey" -m "$bin" -x "$sig" >/dev/null 2>&1; then
		echo "publish-agent.sh: bad signature for $bin" >&2
		return 1
	fi
	case $arch in
	amd64) want_machine=62 ;;
	arm64) want_machine=183 ;;
	esac
	machine=$(elf_machine "$bin") || machine="?"
	if [ "$machine" != "$want_machine" ]; then
		echo "publish-agent.sh: $bin is not $arch (e_machine $machine)" >&2
		return 1
	fi
	# Signature is valid from here on, so executing is safe. The amd64
	# smoke binds the bytes to the published version independently of the
	# file name (the publish host is linux/amd64).
	if [ "$arch" = "amd64" ]; then
		reported=$("$bin" -version 2>/dev/null || echo '?')
		if [ "$reported" != "$ver" ]; then
			echo "publish-agent.sh: binary reports '$reported', want '$ver'" >&2
			return 1
		fi
	fi
	echo "publish-agent.sh: $arch triplet OK ($got)"
	return 0
}

cmd_publish() {
	ver=$1
	indir=$2
	# Publish host precondition (the server target is amd64,
	# docs/decisions.md): the amd64 -version smoke below executes a
	# binary. Refuse clearly instead of failing with Exec format error.
	if [ "$(uname -s)" != "Linux" ] || [ "$(uname -m)" != "x86_64" ]; then
		echo "publish-agent.sh: publishing runs on linux/amd64 only" >&2
		exit 1
	fi
	check_version "$ver" || {
		echo "publish-agent.sh: version '$ver' is not strict N.N.N" >&2
		exit 1
	}
	if [ ! -d "$indir" ]; then
		echo "publish-agent.sh: $indir is not a directory" >&2
		exit 1
	fi
	if [ ! -f "$indir/version" ] || [ "$(cat "$indir/version")" != "$ver" ]; then
		echo "publish-agent.sh: $indir is not a release bundle for $ver" >&2
		exit 1
	fi
	# Closed bundle: exactly the 7 known files, nothing else.
	n=$(find "$indir" -maxdepth 1 -type f | wc -l | tr -d ' ')
	if [ "$n" != "7" ]; then
		echo "publish-agent.sh: $indir holds $n files, want exactly 7" >&2
		exit 1
	fi
	for f in version cadence-agent-linux-amd64 cadence-agent-linux-amd64.sha256 \
		cadence-agent-linux-amd64.minisig cadence-agent-linux-arm64 \
		cadence-agent-linux-arm64.sha256 cadence-agent-linux-arm64.minisig; do
		if [ ! -f "$indir/$f" ]; then
			echo "publish-agent.sh: unexpected bundle content (missing $f)" >&2
			exit 1
		fi
	done
	if [ ! -f "$pubkey" ]; then
		echo "publish-agent.sh: no public key at $pubkey -- refusing unsigned publish" >&2
		exit 1
	fi
	if ! command -v minisign >/dev/null 2>&1; then
		echo "publish-agent.sh: minisign is not installed -- cannot verify, refusing" >&2
		exit 1
	fi
	if [ -e "$dist/agent/v$ver" ]; then
		echo "publish-agent.sh: dist/agent/v$ver already published (immutable, never overwrite)" >&2
		exit 1
	fi

	for arch in $ARCHS; do
		verify_triplet "$arch" "$indir" "$ver" || exit 1
	done

	# Atomic stage: prepare a sibling temp dir (same filesystem as dist/
	# by construction) and rename it once, complete, into place. A
	# failure or interruption before the rename leaves no public trace;
	# the temp lives directly under dist/, never under the served
	# dist/agent/, so even a killed run exposes nothing.
	mkdir -p "$dist/agent"
	tmp="$dist/.staging-v$ver-$$"
	rm -rf "$tmp"
	mkdir -p "$tmp"
	trap 'rm -rf "$tmp"' EXIT INT TERM
	for arch in $ARCHS; do
		cp "$indir/cadence-agent-linux-$arch" \
			"$indir/cadence-agent-linux-$arch.sha256" \
			"$indir/cadence-agent-linux-$arch.minisig" "$tmp/"
	done
	mv "$tmp" "$dist/agent/v$ver"
	trap - EXIT INT TERM

	# Refresh the unversioned install triplet from the just-published
	# amd64 files (the legacy installer fetches fixed names; bootstrap
	# path only). Each file
	# moves via temp + rename; any mixed-version window fails closed
	# because the installer compares the binary against the served
	# checksum.
	src="$dist/agent/v$ver"
	for f in cadence-agent cadence-agent.sha256 cadence-agent.minisig; do
		case $f in
		cadence-agent) s="cadence-agent-linux-amd64" ;;
		cadence-agent.sha256) s="cadence-agent-linux-amd64.sha256" ;;
		cadence-agent.minisig) s="cadence-agent-linux-amd64.minisig" ;;
		esac
		cp "$src/$s" "$dist/agent/.new-$f-$$"
		mv "$dist/agent/.new-$f-$$" "$dist/agent/$f"
	done

	echo "publish-agent.sh: published $ver"
	sha256sum "$dist/agent/v$ver"/cadence-agent-linux-* | sed 's/^/  /'
	echo "  unversioned install triplet refreshed from $ver/amd64"
}

cmd_assets() {
	# Bootstrap assets only: no binaries, no versions, no signatures.
	# Safe to run on every deploy.
	mkdir -p "$dist/agent/systemd"
	install -m 0644 "$repo/scripts/agent-install.sh" "$dist/install.sh"
	for unit in $UNITS; do
		install -m 0644 "$repo/agent/systemd/$unit" "$dist/agent/systemd/$unit"
	done
	caddy_cid=$(docker compose ps -q caddy)
	if [ -z "$caddy_cid" ]; then
		echo "publish-agent.sh: caddy not running -- cannot export the CA" >&2
		exit 1
	fi
	docker compose exec -T caddy \
		cat /data/caddy/pki/authorities/local/root.crt >"$dist/agent/ca.crt"
	echo "publish-agent.sh: staged install.sh + units + CA in $dist"
}

case "${1:-}" in
publish)
	[ $# -eq 3 ] || usage
	cmd_publish "$2" "$3"
	;;
assets)
	[ $# -eq 1 ] || usage
	cmd_assets
	;;
*)
	usage
	;;
esac
