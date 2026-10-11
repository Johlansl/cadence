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
# That defeats signing as a separate step: a root-compromised server builds a malicious
# binary, the operator signs it blindly, and server-side signature checks
# then "verify" the attacker's bytes. Signature verification only proves the
# staged bytes are the signed bytes; it says nothing about a build the
# attacker controlled. So the server is never the source of signed bytes,
# and this script only verifies and stages a bundle it got from elsewhere,
# by one of two paths:
#
#   scripts/publish-agent.sh fetch <version>
#      The normal path. Downloads the 6 release assets (binary, .sha256,
#      .minisig, per arch) for a released version (agent-v<version>) from
#      a GitHub Release -- by default this project's, overridable for a
#      fork -- into a temp directory, writes the local "version" marker
#      itself (the 7th closed-set member: never a release asset, see
#      stage_version below), then runs exactly the same verify-and-stage
#      path as `publish` below. The transport is never trusted: it is
#      plain HTTPS to a release host, hardened in transit (TLS 1.2+,
#      HTTPS enforced through redirects, bounded size and time, hard
#      failure on any HTTP error) but the decision to stage is minisign
#      -V against the trust set alone, not the download succeeding.
#   Both refuse a version older than the newest dist/agent/v<N.N.N> already
#   staged (numeric comparison, no override): the unversioned install triplet
#   is refreshed from the last release staged and must never move backwards.
#   scripts/publish-agent.sh publish <version> <indir>
#      The break-glass and air-gap path, for an operator-assembled bundle
#      (normally produced by scripts/build-agent-release.sh, signed by hand
#      with the offline backup key, see that script's header): verifies and
#      stages a local directory instead of downloading one.
#
# Both paths converge on the same checks once a candidate bundle exists
# (grammar, version binding, closed file set, triplet completeness per
# arch, recomputed SHA-256, minisign -V against the trust set in
# agent/minisign.pub and agent/minisign-backup.pub, amd64 -version AFTER
# the signature verifies, ELF machine per arch) then stage atomically: the
# version directory appears via one rename, and the unversioned triplet is
# refreshed from the just-published amd64 files. Both refuse loudly if the
# version already exists: published versions are immutable, a bad release
# needs a new version, never a republish.
#
#   scripts/publish-agent.sh assets
#      Stage only the unversioned bootstrap assets (installer, units, CA).
#      This is what scripts/deploy.sh runs: backend/frontend deploys never
#      need an agent release or a signature.
#
# Rules, enforced by scripts/test-publish-agent.sh:
# - this script never builds (no compiler, no docker for binaries), never
#   signs (no `minisign -S`) and never reads a private key; it only
#   verifies with the committed public keys, using sh + coreutils + curl +
#   minisign (no Go toolchain needed);
# - downloaded code never runs before its signature verifies: the amd64
#   -version smoke runs only after minisign -V succeeds, on a linux/amd64
#   publish host (the server target is amd64); anything else refuses;
# - unsigned publishing does not exist: a missing, malformed, or
#   non-independent (duplicate key ID) trust set refuses before any
#   verification is attempted (a fork must generate its own keys, it
#   cannot publish unsigned);
# - absent version/arch/triplet member, or any unexpected extra file in
#   the bundle: the version is not published, and Caddy answers 404 for it
#   (no fallback);
# - neither trusted private key is ever present on the Cadence server, not
#   even temporarily. Losing or rotating a key is docs/decisions.md, never
#   disabling signatures.
#
# Environment (advanced/test only):
#   CADENCE_PUBLISH_DIST          dist root to stage into (default: <repo>/dist)
#   CADENCE_PUBLISH_PUBKEY        active trust key (default: <repo>/agent/minisign.pub)
#   CADENCE_PUBLISH_PUBKEY_BACKUP backup trust key (default: <repo>/agent/minisign-backup.pub)
#   CADENCE_FETCH_REPO            GitHub "owner/repo" to fetch releases from
#                                 (default: Johlansl/cadence)
#   CADENCE_FETCH_BASE_URL        release host: exactly https://<host> or
#                                 https://<host>:<port>, nothing else
#                                 (default: https://github.com; test-only
#                                 override)

set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH='' cd -- "$here/.." && pwd)
cd "$repo"

dist=${CADENCE_PUBLISH_DIST:-$repo/dist}
pubkey=${CADENCE_PUBLISH_PUBKEY:-$repo/agent/minisign.pub}
pubkey_backup=${CADENCE_PUBLISH_PUBKEY_BACKUP:-$repo/agent/minisign-backup.pub}

ARCHS="amd64 arm64"
UNITS="cadence-agent.service cadence-agent.timer
cadence-agent-poll.service cadence-agent-poll.timer
cadence-agent-health-check-boot.service cadence-agent-health-check-boot.timer"

# Each temp directory this script creates gets its own dedicated cleanup
# variable, removed quoted and individually: a single space-joined list
# passed unquoted to `rm -rf` would word-split on any space in $dist or
# $TMPDIR (a repo checked out under a path like "/home/bob/my
# projects/cadence" would then remove "/home/bob/my"). A path already
# renamed away (the normal success path for the staging directory) is
# simply absent by then, so removing it again is a silent no-op. One
# process-wide EXIT trap, set once: a function-local `trap ... EXIT`
# would silently replace it instead of composing with it (that bug is
# exactly why this is process-wide, rather than one trap per temp
# directory).
cleanup_stage_tmp=""
cleanup_fetch_tmp=""
cleanup() {
	[ -n "$cleanup_stage_tmp" ] && rm -rf -- "$cleanup_stage_tmp"
	[ -n "$cleanup_fetch_tmp" ] && rm -rf -- "$cleanup_fetch_tmp"
	# Explicit, unconditional: the last `[ -n ... ] && ...` above is false
	# (and so is cleanup's own return status) whenever that particular
	# temp var was never set, e.g. a plain `publish` run never sets
	# cleanup_fetch_tmp. Run as the EXIT trap, that false status must
	# never leak into the script's own exit code.
	return 0
}
trap cleanup EXIT
# INT/TERM must clean up AND terminate: a bare `trap cleanup INT TERM`
# only runs the handler and then resumes the interrupted script instead
# of stopping it. Exit with the conventional 128+signal code afterward.
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

usage() {
	echo "usage: publish-agent.sh fetch <version>             (download + verify + atomic stage)" >&2
	echo "       publish-agent.sh publish <version> <indir>   (verify + atomic stage a local bundle)" >&2
	echo "       publish-agent.sh assets                      (bootstrap assets only)" >&2
	exit 2
}

# Strict canonical N.N.N grammar for publishable versions: suffixes refused,
# and so are leading zeros (a component is "0" or starts with 1-9, "01.2.3"
# refuses like the agent does; see the loop at the end of the function).
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

# version_gt <a> <b>: succeed when canonical N.N.N <a> is strictly newer than
# <b>. Both must already have passed check_version. A longer component is the
# larger number (no leading zeros), so no integer test ever sees a huge value.
version_gt() {
	_ga=$1
	_gb=$2
	for _i in 1 2 3; do
		_ca=${_ga%%.*}
		_cb=${_gb%%.*}
		if [ "${#_ca}" -ne "${#_cb}" ]; then
			[ "${#_ca}" -gt "${#_cb}" ]
			return
		fi
		if [ "$_ca" != "$_cb" ]; then
			[ "$_ca" -gt "$_cb" ]
			return
		fi
		_ga=${_ga#*.}
		_gb=${_gb#*.}
	done
	return 1
}

# Refuse to stage a version older than the newest one already published. The
# unversioned install triplet is refreshed from whatever is staged last, so
# staging an older release would move every later bootstrap backwards. This
# guards the operator against a mistake (the wrong tag, an old bundle); it does
# not protect against a compromised server, which can delete dist/ and reset
# it. There is no override: a fix for an older branch cannot be published from
# a server that already holds a newer release.
check_not_below_published() {
	# check_version uses _v, _v1.._v3 and _rest: keep clear of those names
	floor_newest=""
	for floor_dir in "$dist"/agent/v*; do
		[ -d "$floor_dir" ] || continue
		floor_ver=${floor_dir##*/v}
		check_version "$floor_ver" || continue
		if [ -z "$floor_newest" ] || version_gt "$floor_ver" "$floor_newest"; then floor_newest=$floor_ver; fi
	done
	if [ -n "$floor_newest" ] && version_gt "$floor_newest" "$1"; then
		echo "publish-agent.sh: version $1 is older than the newest published version $floor_newest (dist/agent/v$floor_newest): refusing, the unversioned install triplet must never move backwards" >&2
		exit 1
	fi
}

# Print the ELF e_machine of a file (amd64 = 62, arm64 = 183), or fail.
# Needs only od: no Go toolchain, no docker, no `file`.
elf_machine() {
	magic=$(od -A n -t x1 -N 4 "$1" 2>/dev/null | tr -d ' \n') || return 1
	[ "$magic" = "7f454c46" ] || return 1
	od -A n -t u2 -j 18 -N 2 "$1" 2>/dev/null | tr -d ' \n'
}

# Extract and validate the 16-hex-char key ID from a minisign .pub file's
# own comment line, on stdout, without needing the private key or the Go
# library: exactly two lines, the expected comment prefix, a 16-hex-char
# ID, and a key body of the length every minisign Ed25519 public key
# encodes to. A structural check only (same spirit as the SHA-256 hex
# check above), not a substitute for the minisign -V calls that follow: a
# key that passes here but is otherwise bogus simply never verifies any
# real signature.
pubkey_id() {
	f=$1
	if [ ! -f "$f" ]; then
		echo "publish-agent.sh: missing public key $f" >&2
		return 1
	fi
	prefix="untrusted comment: minisign public key "
	first=$(sed -n '1p' "$f")
	second=$(sed -n '2p' "$f")
	third=$(sed -n '3p' "$f")
	case $first in
	"$prefix"*) id=${first#"$prefix"} ;;
	*)
		echo "publish-agent.sh: $f is not a well-formed minisign public key (bad comment line)" >&2
		return 1
		;;
	esac
	if [ "${#id}" -ne 16 ]; then
		echo "publish-agent.sh: $f is not a well-formed minisign public key (key ID is not 16 hex characters)" >&2
		return 1
	fi
	case $id in
	*[!0-9A-Fa-f]*)
		echo "publish-agent.sh: $f is not a well-formed minisign public key (key ID is not hex)" >&2
		return 1
		;;
	esac
	if [ "${#second}" -ne 56 ]; then
		echo "publish-agent.sh: $f is not a well-formed minisign public key (unexpected key body length)" >&2
		return 1
	fi
	if [ -n "$third" ]; then
		echo "publish-agent.sh: $f is not a well-formed minisign public key (extra content after line 2)" >&2
		return 1
	fi
	# The comment is untrusted text: the ID this script uses and logs comes
	# from the key bytes. Line 2 is base64 of 42 bytes: "Ed", the 8 byte key
	# ID (stored least significant byte first), the 32 byte Ed25519 key.
	case $second in
	RW*) ;;
	*)
		echo "publish-agent.sh: $f is not a well-formed minisign public key (key line does not start with RW)" >&2
		return 1
		;;
	esac
	case $second in
	*[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/]*)
		echo "publish-agent.sh: $f is not a well-formed minisign public key (key line is not base64)" >&2
		return 1
		;;
	esac
	raw=$(printf '%s' "$second" | base64 -d 2>/dev/null | od -An -tx1 2>/dev/null | tr -d ' \n') || raw=""
	if [ "${#raw}" -ne 84 ]; then
		echo "publish-agent.sh: $f is not a well-formed minisign public key (key line does not decode to 42 bytes)" >&2
		return 1
	fi
	case $raw in
	4564*) ;;
	*)
		echo "publish-agent.sh: $f is not a well-formed minisign public key (not an Ed25519 key)" >&2
		return 1
		;;
	esac
	computed=$(printf '%s' "$raw" | cut -c5-20 |
		sed 's/^\(..\)\(..\)\(..\)\(..\)\(..\)\(..\)\(..\)\(..\)$/\8\7\6\5\4\3\2\1/' |
		tr 'a-f' 'A-F')
	shown=$(printf '%s' "$id" | tr 'a-f' 'A-F')
	if [ "$computed" != "$shown" ]; then
		echo "publish-agent.sh: $f is not a well-formed minisign public key (the key ID in its comment, $id, is not the ID of the key, $computed)" >&2
		return 1
	fi
	printf '%s' "$computed"
}

# Refuse to start if minisign is missing, either trusted public key is
# absent or malformed, or the two keys are not independent (same key ID):
# a "trust set" that collapses to one effective key, or to zero, must fail
# loud before any verification is attempted, never fall back silently.
# Called first thing by both publish and fetch, before anything else.
check_trust_keys() {
	if ! command -v minisign >/dev/null 2>&1; then
		echo "publish-agent.sh: minisign is not installed -- install the minisign package (e.g. 'apt-get install minisign') and retry" >&2
		exit 1
	fi
	active_id=$(pubkey_id "$pubkey") || exit 1
	backup_id=$(pubkey_id "$pubkey_backup") || exit 1
	if [ "$active_id" = "$backup_id" ]; then
		echo "publish-agent.sh: active and backup public keys share key ID $active_id -- refusing (they must be independent)" >&2
		exit 1
	fi
}

# Verify a signature against every trusted key in turn, accepting on the
# first that verifies; refuse only once none has. Logs which key ID
# verified and the signature's trusted comment for operator visibility --
# like the agent, this script never treats the comment as version truth,
# only as a label.
verify_signature() {
	bin=$1
	sig=$2
	for p in "$pubkey" "$pubkey_backup"; do
		if out=$(minisign -V -p "$p" -m "$bin" -x "$sig" 2>&1); then
			id=$(pubkey_id "$p") || id="?"
			comment=$(printf '%s\n' "$out" | sed -n 's/^Trusted comment: //p')
			echo "publish-agent.sh: signature OK for $bin (key $id)"
			[ -n "$comment" ] && echo "publish-agent.sh:   trusted comment: $comment"
			return 0
		fi
	done
	echo "publish-agent.sh: no trusted key verified the signature for $bin" >&2
	return 1
}

# Parse a .sha256 file with exactly the same rules as the agent's
# parseSHA256File (agent/internal/upgrade/install.go), so a file this
# script accepts is guaranteed to be one the agent will later accept too:
# at most one trailing newline is stripped, then the remainder must
# contain no other newline and no carriage return at all (this rejects
# CRLF, a second line, or any stray byte after it); the result must split
# on exactly two literal spaces into a 64-char lowercase hex hash and a
# name field equal to $2 exactly. Prints the hash on stdout, or refuses.
parse_sha256_file() {
	shafile=$1
	wantname=$2
	nl=$(printf '\nX'); nl=${nl%X}
	cr=$(printf '\rX'); cr=${cr%X}
	content=$(cat "$shafile"; printf X) || return 1
	content=${content%X}
	content=${content%"$nl"}
	case $content in
	*"$nl"*|*"$cr"*)
		echo "publish-agent.sh: $shafile must be a single line" >&2
		return 1
		;;
	esac
	case $content in
	*"  "*) ;;
	*)
		echo "publish-agent.sh: $shafile must name $wantname" >&2
		return 1
		;;
	esac
	hash=${content%%"  "*}
	name=${content#*"  "}
	if [ "$name" != "$wantname" ]; then
		echo "publish-agent.sh: $shafile must name $wantname" >&2
		return 1
	fi
	if [ "${#hash}" -ne 64 ]; then
		echo "publish-agent.sh: $shafile is not a SHA-256" >&2
		return 1
	fi
	case $hash in
	*[!0-9a-f]*)
		echo "publish-agent.sh: $shafile is not hex" >&2
		return 1
		;;
	esac
	printf '%s' "$hash"
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
	want=$(parse_sha256_file "$sha" "cadence-agent-linux-$arch") || return 1
	got=$(sha256sum "$bin" | awk '{print $1}')
	if [ "$want" != "$got" ]; then
		echo "publish-agent.sh: checksum mismatch for $bin" >&2
		echo "  file says $want" >&2
		echo "  actual   $got" >&2
		return 1
	fi
	verify_signature "$bin" "$sig" || return 1
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

# Shared verify+stage path for both `publish` (operator-supplied indir) and
# `fetch` (a freshly downloaded indir): validate the bundle is exactly the
# closed 7-file set for $ver, verify every triplet against the trust set,
# then atomically stage it into dist/agent/v$ver and refresh the
# unversioned install triplet. fetch builds its indir to look exactly like
# a publish bundle and hands it here unchanged: this is the one place
# either path can reach dist/, so there is nothing to duplicate or drift.
stage_version() {
	ver=$1
	indir=$2
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
	rm -rf -- "$tmp"
	mkdir -p "$tmp"
	cleanup_stage_tmp=$tmp
	for arch in $ARCHS; do
		cp "$indir/cadence-agent-linux-$arch" \
			"$indir/cadence-agent-linux-$arch.sha256" \
			"$indir/cadence-agent-linux-$arch.minisig" "$tmp/"
	done
	mv "$tmp" "$dist/agent/v$ver"

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

# Publish host precondition (the server target is amd64, docs/decisions.md):
# the amd64 -version smoke in verify_triplet executes a binary. Refuse
# clearly instead of failing with Exec format error. Shared by publish and
# fetch.
check_publish_host() {
	if [ "$(uname -s)" != "Linux" ] || [ "$(uname -m)" != "x86_64" ]; then
		echo "publish-agent.sh: publishing runs on linux/amd64 only" >&2
		exit 1
	fi
}

cmd_publish() {
	ver=$1
	indir=$2
	check_trust_keys
	check_publish_host
	check_version "$ver" || {
		echo "publish-agent.sh: version '$ver' is not strict N.N.N" >&2
		exit 1
	}
	if [ -e "$dist/agent/v$ver" ]; then
		echo "publish-agent.sh: dist/agent/v$ver already published (immutable, never overwrite)" >&2
		exit 1
	fi
	check_not_below_published "$ver"
	stage_version "$ver" "$indir"
}

# Strict owner/repo grammar for CADENCE_FETCH_REPO overrides: letters,
# digits, dot, underscore, hyphen in each segment, exactly one slash, no
# empty segment. Defense in depth against a stray character reaching a
# constructed URL; GitHub's own naming rules are a superset of this.
check_fetch_repo() {
	case $1 in
	*[!A-Za-z0-9._/-]*|"") return 1 ;;
	esac
	case $1 in
	*/*/*|/*|*/) return 1 ;;
	*/*) ;;
	*) return 1 ;;
	esac
	return 0
}

# Strict grammar for CADENCE_FETCH_BASE_URL: exactly https://<host> or
# https://<host>:<port>, nothing else. No userinfo (no "@"), no path, no
# trailing slash, no query, no fragment -- the release path itself is
# built separately from $repo_slug/releases/download/..., so this
# variable names only the host. Host restricted to [A-Za-z0-9.-]; port,
# if present, a plain decimal 1-65535.
check_fetch_base_url() {
	case $1 in
	https://*) ;;
	*) return 1 ;;
	esac
	rest=${1#https://}
	case $rest in
	*[@/?'#']*) return 1 ;;
	esac
	case $rest in
	*:*)
		host=${rest%%:*}
		port=${rest#*:}
		case $port in
		*:*) return 1 ;;
		*[!0-9]*|"") return 1 ;;
		esac
		if [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
			return 1
		fi
		;;
	*)
		host=$rest
		;;
	esac
	case $host in
	*[!A-Za-z0-9.-]*|"") return 1 ;;
	esac
	return 0
}

# Download one URL into a destination path: -q first so curl never reads
# ~/.curlrc (a local config file must not be able to silently change how
# this verification-critical download behaves), HTTPS only even across a
# redirect (GitHub release asset downloads redirect to a different host),
# TLS 1.2 minimum, bounded size and time, hard failure on any HTTP error
# status. This is anti-corruption and a DoS bound on an untrusted
# transport, never a substitute for the minisign verification that follows
# in stage_version: curl succeeding proves nothing about the bytes.
fetch_file() {
	url=$1
	out=$2
	max=$3
	if ! curl -q -fsS -L --proto '=https' --proto-redir '=https' --tlsv1.2 \
		--max-filesize "$max" --connect-timeout 10 --max-time 120 \
		-o "$out" "$url"; then
		echo "publish-agent.sh: download failed: $url" >&2
		return 1
	fi
}

cmd_fetch() {
	ver=$1
	check_trust_keys
	check_publish_host
	check_version "$ver" || {
		echo "publish-agent.sh: version '$ver' is not strict N.N.N" >&2
		exit 1
	}
	if [ -e "$dist/agent/v$ver" ]; then
		echo "publish-agent.sh: dist/agent/v$ver already published (immutable, never overwrite)" >&2
		exit 1
	fi
	repo_slug=${CADENCE_FETCH_REPO:-Johlansl/cadence}
	check_fetch_repo "$repo_slug" || {
		echo "publish-agent.sh: CADENCE_FETCH_REPO '$repo_slug' is not a valid owner/repo" >&2
		exit 1
	}
	base=${CADENCE_FETCH_BASE_URL:-https://github.com}
	check_fetch_base_url "$base" || {
		echo "publish-agent.sh: CADENCE_FETCH_BASE_URL '$base' must be exactly https://<host> or https://<host>:<port> (no userinfo, path, query or fragment)" >&2
		exit 1
	}
	tag="agent-v$ver"
	check_not_below_published "$ver"

	tmp=$(mktemp -d "${TMPDIR:-/tmp}/publish-agent-fetch.XXXXXX")
	cleanup_fetch_tmp=$tmp

	for arch in $ARCHS; do
		name="cadence-agent-linux-$arch"
		fetch_file "$base/$repo_slug/releases/download/$tag/$name" \
			"$tmp/$name" 67108864 || exit 1
		# HTTP carries no POSIX permission bits: a local bundle handed to
		# `publish` already has its executable bit (it came from a real
		# build), but a freshly downloaded file never does. The later
		# -version probe (verify_triplet, after the signature verifies)
		# needs to exec this file.
		chmod +x "$tmp/$name"
		fetch_file "$base/$repo_slug/releases/download/$tag/$name.sha256" \
			"$tmp/$name.sha256" 4096 || exit 1
		fetch_file "$base/$repo_slug/releases/download/$tag/$name.minisig" \
			"$tmp/$name.minisig" 4096 || exit 1
	done
	# The 7th closed-set member: a plain local marker, never a release
	# asset. stage_version only uses it to bind this directory to the
	# $ver it was asked to stage; fetch already knows that from its own
	# argument, so it writes the file itself instead of fetching one.
	printf '%s\n' "$ver" >"$tmp/version"

	stage_version "$ver" "$tmp"
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
fetch)
	[ $# -eq 2 ] || usage
	cmd_fetch "$2"
	;;
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
