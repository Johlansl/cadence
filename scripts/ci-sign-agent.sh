#!/bin/sh
# ci-sign-agent.sh -- sign the agent release binaries in CI, then self-verify.
#
#   ci-sign-agent.sh <version> <pubkey> <seckey> <dir> <arch>=<sha256> ...
#
#   version  release version, the tag without its "agent-v" prefix; strict
#            canonical N.N.N only (0.15.0)
#   pubkey   public key file the signatures must verify against (CI passes
#            agent/minisign.pub from the checkout at the tag)
#   seckey   unencrypted minisign secret key file (CI writes it under
#            $RUNNER_TEMP for the one step that needs it)
#   dir      directory holding cadence-agent-linux-<arch>
#   arch=hash  amd64 or arm64, with the SHA-256 the build job reported
#
# Called by the `sign` job of .github/workflows/release.yml, which does
# nothing else than call this. In order:
#   1. every binary must hash to the value the build job reported; on any
#      mismatch nothing is signed (the artifact changed between jobs);
#   2. each binary is signed to <binary>.minisig with the trusted comment
#        cadence-agent <version> linux/<arch> sha256:<hash>
#      The comment is for human audit only: the agent never parses it (its
#      sha256, ELF/arch and -version checks are what it relies on);
#   3. each signature is verified against <pubkey>, and its trusted comment
#      is checked byte for byte. A secret key that does not match the
#      committed public key fails here, before anything is published.
# If any step fails, every .minisig this run produced is removed and the
# script exits non-zero. The secret key is only ever passed to minisign as
# a file path; its content is never read or printed here.

set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# The trusted comment format has one definition, shared with
# build-agent-release.sh (break-glass signing).
# shellcheck source=scripts/agent-sign-lib.sh
. "$here/agent-sign-lib.sh"

die() {
	echo "ci-sign-agent: $*" >&2
	exit 1
}

# Strict canonical N.N.N (check_version): one definition, shared with
# publish-agent.sh and check-release-source.sh.
# shellcheck source=scripts/agent-version-lib.sh
. "$here/agent-version-lib.sh"

if [ "$#" -lt 5 ]; then
	echo "usage: ci-sign-agent.sh <version> <pubkey> <seckey> <dir> <arch>=<sha256> ..." >&2
	exit 2
fi

version=$1
pubkey=$2
seckey=$3
dir=$4
shift 4

check_version "$version" || die "version '$version' is not strict canonical N.N.N"
[ -f "$pubkey" ] || die "public key file not found: $pubkey"
[ -f "$seckey" ] || die "secret key file not found: $seckey"
[ -s "$seckey" ] || die "secret key file is empty: $seckey"
[ -d "$dir" ] || die "not a directory: $dir"
command -v minisign >/dev/null 2>&1 || die "minisign not found in PATH"

# remove_sigs -- drop every signature this run may have produced.
remove_sigs() {
	for spec in "$@"; do
		rm -f -- "$dir/cadence-agent-linux-${spec%%=*}.minisig"
	done
}

# 1. Validate every argument and check every hash before signing anything.
seen=" "
for spec in "$@"; do
	case $spec in
	*=*) ;;
	*) die "bad argument '$spec' (want <arch>=<sha256>)" ;;
	esac
	arch=${spec%%=*}
	want=${spec#*=}
	case $arch in
	amd64 | arm64) ;;
	*) die "unsupported arch '$arch' (want amd64 or arm64)" ;;
	esac
	case $seen in
	*" $arch "*) die "arch '$arch' given twice" ;;
	esac
	seen="$seen$arch "
	case $want in
	*[!0-9a-f]*) die "expected sha256 for $arch is not 64 lowercase hex characters" ;;
	esac
	[ "${#want}" -eq 64 ] || die "expected sha256 for $arch is not 64 lowercase hex characters"
	bin=$dir/cadence-agent-linux-$arch
	[ -f "$bin" ] || die "binary not found: $bin"
	got=$(sha256sum "$bin" | cut -d' ' -f1)
	[ "$got" = "$want" ] || die "sha256 mismatch for $bin: expected $want, got $got; refusing to sign"
done

# 2. Sign. stdin is closed so a key that unexpectedly wants a password fails
# instead of waiting for input.
for spec in "$@"; do
	arch=${spec%%=*}
	want=${spec#*=}
	bin=$dir/cadence-agent-linux-$arch
	rm -f -- "$bin.minisig"
	if ! minisign -S -s "$seckey" -x "$bin.minisig" \
		-t "$(agent_trusted_comment "$version" "$arch" "$want")" \
		-m "$bin" </dev/null >/dev/null; then
		remove_sigs "$@"
		die "signing failed for $bin"
	fi
done

# 3. Self-verify against the committed public key, trusted comment included.
for spec in "$@"; do
	arch=${spec%%=*}
	want=${spec#*=}
	bin=$dir/cadence-agent-linux-$arch
	if ! minisign -V -p "$pubkey" -x "$bin.minisig" -m "$bin" -q </dev/null; then
		remove_sigs "$@"
		die "signature for $bin does not verify against $pubkey (secret key and committed public key out of sync?)"
	fi
	line=$(sed -n 3p "$bin.minisig")
	if [ "$line" != "trusted comment: $(agent_trusted_comment "$version" "$arch" "$want")" ]; then
		remove_sigs "$@"
		die "unexpected trusted comment in $bin.minisig"
	fi
	echo "ci-sign-agent: signed and verified $bin.minisig"
done
