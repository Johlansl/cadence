#!/bin/sh
# Prove the versioned, immutable, signed agent publish flow (fetch and publish).
#
# Part 0 (static, no tools): publish-agent.sh never signs and never reads
#   a private key.
# Part 1 (fixture bytes; needs sh, coreutils, minisign): the full
#   publish/verify/atomicity/immutability matrix against temp dirs, with
#   HOME pointed at an empty dir so any private-key lookup fails loudly.
#   Runs in both CIs.
# Part 2 (real docker builds; only when DOCKER_TESTS=1): export smoke
#   (-version, GOARCH per binary) plus end-to-end publish of real
#   binaries. GitHub CI + local runs (GitLab `scripts` has no docker).
#
# Only ephemeral test keys, generated in temp dirs. No repo file is
# modified (overrides: CADENCE_PUBLISH_DIST, CADENCE_PUBLISH_PUBKEY,
# CADENCE_PUBLISH_PUBKEY_BACKUP, CADENCE_FETCH_REPO, CADENCE_FETCH_BASE_URL).
#
#   sh scripts/test-publish-agent.sh
#   DOCKER_TESTS=1 sh scripts/test-publish-agent.sh
#
# Requires: minisign installed (apk add / apt-get install minisign).
# The fetch tests additionally need python3 and openssl to run a throwaway
# local HTTPS server (no real network to GitHub): a real curl client
# against a real TLS listener, trusted for the test the same way curl
# lets any caller trust a non-default CA, via CURL_CA_BUNDLE -- never a
# test-only flag added to publish-agent.sh itself.

set -eu

cd "$(dirname -- "$0")/.."

failed=0
pass=0
ok() {
	pass=$((pass + 1))
	echo "publish-test: ok: $1"
}
fail() {
	failed=$((failed + 1))
	echo "publish-test: FAIL: $1" >&2
}
expect_fail() {
	desc=$1
	shift
	if "$@" >/dev/null 2>&1; then
		fail "$desc published (must refuse)"
	else
		ok "$desc refused"
	fi
}

# --- part 0: publish-agent.sh must not sign or read a private key -------
# Match real invocations (start of logical line), not comments documenting
# the ban.
if grep -qE '^[[:space:]]*minisign -S' scripts/publish-agent.sh; then
	fail "publish-agent.sh invokes minisign -S (server must never sign)"
else
	ok "publish-agent.sh never signs"
fi
if grep -q '\.cadence/minisign\.key' scripts/publish-agent.sh; then
	fail "publish-agent.sh references the server private key path"
else
	ok "publish-agent.sh never references the server private key"
fi
if grep -q 'git describe' scripts/publish-agent.sh; then
	fail "publish-agent.sh still derives versions from git (must be explicit)"
else
	ok "publish-agent.sh takes explicit versions only"
fi
# The server must not build release bytes at all: no compiler, no build
# container, no Go image reference anywhere in the publish path.
for token in 'go build' 'docker run' 'golang:'; do
	if grep -qF "$token" scripts/publish-agent.sh; then
		fail "publish-agent.sh mentions '$token' (server must not build)"
	else
		ok "publish-agent.sh mentions no '$token'"
	fi
done
if sh scripts/publish-agent.sh export 9.9.0 /tmp/nope 2>/dev/null; then
	fail "publish-agent.sh export still exists (server must not build)"
else
	ok "publish-agent.sh has no export subcommand"
fi
if [ -f scripts/build-agent-release.sh ]; then
	ok "build-agent-release.sh exists (break-glass build tool)"
else
	fail "scripts/build-agent-release.sh is missing"
fi

command -v minisign >/dev/null 2>&1 || {
	echo "publish-test: minisign is not installed" >&2
	exit 1
}
command -v git >/dev/null 2>&1 || {
	echo "publish-test: git is not installed" >&2
	exit 1
}

# --- part S: release-source identity (temp git repos, no docker) -------
S=$(mktemp -d)
# mkrepo <name>: repo with two CHANGELOG headings and one tracked file.
mkrepo() {
	r=$S/$1
	mkdir -p "$r/agent"
	printf '## 9.9.2\n\n## 9.9.1\n' >"$r/agent/CHANGELOG.md"
	printf 'keep\n' >"$r/agent/keep.txt"
	(cd "$r" && git init -q 2>/dev/null \
		&& git -c user.email=t@t -c user.name=t add -A \
		&& git -c user.email=t@t -c user.name=t commit -qm init)
	printf '%s' "$r"
}
gcommit() {
	(cd "$1" && git -c user.email=t@t -c user.name=t commit -qam "$2")
}
checksrc() {
	CADENCE_RELEASE_REPO="$1" sh scripts/check-release-source.sh "$2" >/dev/null 2>&1
}

r1=$(mkrepo ok) && (cd "$r1" && git tag agent-v9.9.1)
if checksrc "$r1" 9.9.1; then
	ok "source happy path: HEAD on agent-v9.9.1, clean"
else
	fail "source happy path refused"
fi

r2=$(mkrepo descendant) && (cd "$r2" && git tag agent-v9.9.1 \
	&& echo more >>agent/keep.txt && gcommit "$r2" second)
if checksrc "$r2" 9.9.1; then
	fail "commit after tag accepted"
else
	ok "commit after tag refused"
fi

r3=$(mkrepo wrongtag) && (cd "$r3" && git tag agent-v9.9.1 \
	&& echo more >>agent/keep.txt && gcommit "$r3" second && git tag agent-v9.9.2 \
	&& git checkout -q agent-v9.9.1)
if checksrc "$r3" 9.9.2; then
	fail "version/tag mismatch accepted"
else
	ok "version/tag mismatch refused"
fi

r4=$(mkrepo dirty) && (cd "$r4" && git tag agent-v9.9.1 \
	&& echo dirty >>agent/keep.txt)
if checksrc "$r4" 9.9.1; then
	fail "dirty worktree accepted"
else
	ok "dirty worktree refused"
fi

r5=$(mkrepo staged) && (cd "$r5" && git tag agent-v9.9.1 \
	&& echo staged >>agent/keep.txt && git add agent/keep.txt)
if checksrc "$r5" 9.9.1; then
	fail "staged modification accepted"
else
	ok "staged modification refused"
fi

r6=$(mkrepo notag)
if checksrc "$r6" 9.9.1; then
	fail "missing tag accepted"
else
	ok "missing tag refused"
fi

r7=$(mkrepo untracked) && (cd "$r7" && git tag agent-v9.9.1 \
	&& echo scratch >agent/scratch.txt)
if checksrc "$r7" 9.9.1; then
	fail "untracked file under agent/ accepted"
else
	ok "untracked file under agent/ refused"
fi

r8=$(mkrepo rootscratch) && (cd "$r8" && git tag agent-v9.9.1 \
	&& echo scratch >scratch.txt)
if checksrc "$r8" 9.9.1; then
	ok "root scratch file tolerated"
else
	fail "root scratch file refused"
fi

r9=$(mkrepo ignored) && (cd "$r9" \
	&& printf '/agent/bin/\n' >.gitignore && git add .gitignore \
	&& git -c user.email=t@t -c user.name=t commit -qm ignore \
	&& git tag agent-v9.9.1 && mkdir -p agent/bin && echo x >agent/bin/x)
if checksrc "$r9" 9.9.1; then
	ok "ignored build output tolerated"
else
	fail "ignored build output refused"
fi

if checksrc "$r1" 1.2; then
	fail "bad grammar accepted by source check"
else
	ok "bad grammar refused by source check"
fi
rm -rf "$S"

# --- part 1: fixture matrix ---------------------------------------------
T=$(mktemp -d)
trap 'rm -rf "$T" "$S"' EXIT INT TERM
export HOME="$T/emptyhome"
mkdir -p "$HOME"
DIST="$T/dist"
mkdir -p "$DIST/agent"
PUB="$T/keys/test.pub"
KEY="$T/keys/test.key"
PUB_BACKUP="$T/keys/backup.pub"
KEY_BACKUP="$T/keys/backup.key"
mkdir -p "$T/keys"
minisign -G -W -p "$PUB" -s "$KEY" >/dev/null 2>&1
minisign -G -W -p "$PUB_BACKUP" -s "$KEY_BACKUP" >/dev/null 2>&1
export CADENCE_PUBLISH_DIST="$DIST"
export CADENCE_PUBLISH_PUBKEY="$PUB"
export CADENCE_PUBLISH_PUBKEY_BACKUP="$PUB_BACKUP"

# Emit one byte ($1, decimal) on stdout. POSIX sh + printf only.
byte() { printf '%b' "$(printf '\\0%o' "$1")"; }

# mkstub <string> <outfile>: minimal static x86_64 ELF that prints $1 on
# stdout and exits 0 (test fixture only: lets the suite exercise the
# server's ELF-machine and -version checks with no toolchain).
mkstub() {
	msg=$1
	out=$2
	len=${#msg}
	total=$((154 + len))
	{
		printf '\177ELF\002\001\001\000\000\000\000\000\000\000\000\000'
		printf '\002\000\076\000\001\000\000\000'
		printf '\170\000\100\000\000\000\000\000'
		printf '\100\000\000\000\000\000\000\000'
		printf '\000\000\000\000\000\000\000\000'
		printf '\000\000\000\000\100\000\070\000\001\000\000\000\000\000\000\000'
		printf '\001\000\000\000\005\000\000\000'
		printf '\000\000\000\000\000\000\000\000'
		printf '\000\000\100\000\000\000\000\000'
		printf '\000\000\000\000\000\000\000\000'
		b0=$((total % 256)); b1=$(((total / 256) % 256))
		byte "$b0"; byte "$b1"
		printf '\000\000\000\000\000\000'
		byte "$b0"; byte "$b1"
		printf '\000\000\000\000\000\000'
		printf '\000\020\000\000\000\000\000\000'
		l0=$((len % 256))
		printf '\270\001\000\000\000\277\001\000\000\000'
		printf '\110\215\065\021\000\000\000'
		printf '\272'
		byte "$l0"
		printf '\000\000\000\017\005'
		printf '\270\074\000\000\000\110\061\377\017\005'
		printf '%s' "$msg"
	} >"$out"
	chmod +x "$out"
}

# mksigned <version> <dir>: amd64 stub printing <version>, arm64 twin
# (same bytes, e_machine patched to 183), sha256, version file and valid
# test signatures.
mksigned() {
	ver=$1
	dir=$2
	mkdir -p "$dir"
	mkstub "$ver" "$dir/cadence-agent-linux-amd64"
	cp "$dir/cadence-agent-linux-amd64" "$dir/cadence-agent-linux-arm64"
	printf '\267\000' | dd of="$dir/cadence-agent-linux-arm64" bs=1 seek=18 \
		conv=notrunc 2>/dev/null
	(
		cd "$dir"
		sha256sum cadence-agent-linux-amd64 >cadence-agent-linux-amd64.sha256
		sha256sum cadence-agent-linux-arm64 >cadence-agent-linux-arm64.sha256
		printf '%s\n' "$ver" >version
	)
	minisign -S -s "$KEY" -m "$dir/cadence-agent-linux-amd64" \
		-t "test $ver" >/dev/null 2>&1
	minisign -S -s "$KEY" -m "$dir/cadence-agent-linux-arm64" \
		-t "test $ver" >/dev/null 2>&1
}

snapshot() {
	(cd "$DIST" && find . -type f | sort | xargs sha256sum)
}

# 1. happy path
mksigned 9.9.1 "$T/b1"
if sh scripts/publish-agent.sh publish 9.9.1 "$T/b1" >/dev/null 2>&1; then
	n=$(find "$DIST/agent/v9.9.1" -type f | wc -l)
	if [ "$n" = "6" ]; then
		ok "happy path stages 6 files"
	else
		fail "happy path staged $n files, want 6"
	fi
	if cmp -s "$T/b1/cadence-agent-linux-amd64" "$DIST/agent/v9.9.1/cadence-agent-linux-amd64" \
		&& cmp -s "$T/b1/cadence-agent-linux-arm64" "$DIST/agent/v9.9.1/cadence-agent-linux-arm64"; then
		ok "staged bytes are byte-identical to the bundle"
	else
		fail "staged bytes differ from the bundle"
	fi
	if cmp -s "$DIST/agent/v9.9.1/cadence-agent-linux-amd64" "$DIST/agent/cadence-agent" \
		&& cmp -s "$DIST/agent/v9.9.1/cadence-agent-linux-amd64.sha256" "$DIST/agent/cadence-agent.sha256" \
		&& cmp -s "$DIST/agent/v9.9.1/cadence-agent-linux-amd64.minisig" "$DIST/agent/cadence-agent.minisig"; then
		ok "unversioned triplet refreshed from amd64"
	else
		fail "unversioned triplet mismatch"
	fi
else
	fail "happy path publish failed"
fi

# 2. tampered binary after signing
mksigned 9.9.2 "$T/b2"
printf 'x' | dd of="$T/b2/cadence-agent-linux-amd64" bs=1 seek=100 conv=notrunc 2>/dev/null
expect_fail "tampered binary" sh scripts/publish-agent.sh publish 9.9.2 "$T/b2"
if [ ! -e "$DIST/agent/v9.9.2" ]; then
	ok "no public dir after bad signature"
else
	fail "partial v9.9.2 exposed"
fi

# 3. corrupt checksum file
mksigned 9.9.3 "$T/b3"
echo "deadbeef  cadence-agent-linux-amd64" >"$T/b3/cadence-agent-linux-amd64.sha256"
expect_fail "corrupt sha256" sh scripts/publish-agent.sh publish 9.9.3 "$T/b3"
if [ ! -e "$DIST/agent/v9.9.3" ]; then
	ok "no public dir after bad checksum"
else
	fail "partial v9.9.3 exposed"
fi

# 4. incomplete triplets
mksigned 9.9.4 "$T/b4"
rm "$T/b4/cadence-agent-linux-amd64.minisig"
expect_fail "missing .minisig" sh scripts/publish-agent.sh publish 9.9.4 "$T/b4"
if [ ! -e "$DIST/agent/v9.9.4" ]; then
	ok "no public dir when .minisig missing"
else
	fail "partial v9.9.4 exposed"
fi
mksigned 9.9.5 "$T/b5"
rm "$T/b5/cadence-agent-linux-arm64.sha256"
expect_fail "missing .sha256" sh scripts/publish-agent.sh publish 9.9.5 "$T/b5"
if [ ! -e "$DIST/agent/v9.9.5" ]; then
	ok "no public dir when .sha256 missing"
else
	fail "partial v9.9.5 exposed"
fi
mksigned 9.9.6 "$T/b6"
rm "$T/b6"/cadence-agent-linux-arm64*
expect_fail "missing arch" sh scripts/publish-agent.sh publish 9.9.6 "$T/b6"
if [ ! -e "$DIST/agent/v9.9.6" ]; then
	ok "no public dir when arch missing"
else
	fail "partial v9.9.6 exposed"
fi

# 5. immutability: republish refuses, first version byte-identical
before=$(snapshot)
expect_fail "republish 9.9.1" sh scripts/publish-agent.sh publish 9.9.1 "$T/b1"
after=$(snapshot)
if [ "$before" = "$after" ]; then
	ok "republish refused, staged tree byte-identical"
else
	fail "republish changed the staged tree"
fi

# 6. atomicity: late failure leaves nothing behind, unversioned intact
mksigned 9.9.7 "$T/b7"
printf 'x' | dd of="$T/b7/cadence-agent-linux-arm64" bs=1 seek=200 conv=notrunc 2>/dev/null
unv_before=$(sha256sum "$DIST/agent/cadence-agent" "$DIST/agent/cadence-agent.sha256" \
	"$DIST/agent/cadence-agent.minisig")
expect_fail "bad arm64 late" sh scripts/publish-agent.sh publish 9.9.7 "$T/b7"
if [ ! -e "$DIST/agent/v9.9.7" ]; then
	ok "no public dir after late failure"
else
	fail "partial v9.9.7 exposed"
fi
if ls "$DIST"/.staging-* >/dev/null 2>&1; then
	fail "staging temp left behind"
else
	ok "no staging temp left behind"
fi
unv_after=$(sha256sum "$DIST/agent/cadence-agent" "$DIST/agent/cadence-agent.sha256" \
	"$DIST/agent/cadence-agent.minisig")
if [ "$unv_before" = "$unv_after" ]; then
	ok "unversioned triplet untouched by failed publish"
else
	fail "failed publish touched the unversioned triplet"
fi

# 7. version binding + grammar
mksigned 9.9.8 "$T/b8"
expect_fail "version mismatch" sh scripts/publish-agent.sh publish 9.9.9 "$T/b8"
expect_fail "bad grammar 1.2" sh scripts/publish-agent.sh publish 1.2 "$T/b8"
expect_fail "bad grammar v1.2.3" sh scripts/publish-agent.sh publish v1.2.3 "$T/b8"
if [ ! -e "$DIST/agent/v9.9.9" ] && [ ! -e "$DIST/agent/v1.2" ]; then
	ok "no public dir after version refusal"
else
	fail "public dir after version refusal"
fi

# 8. no public key configured: unsigned publish impossible
CADENCE_PUBLISH_PUBKEY="$T/keys/absent.pub" \
	expect_fail "missing pubkey" sh scripts/publish-agent.sh publish 9.9.8 "$T/b8"

# 9. closed bundle: any unexpected extra file refuses the whole version
mksigned 9.9.10 "$T/b10"
printf '#!/bin/sh\necho pwned\n' >"$T/b10/command.sh"
chmod +x "$T/b10/command.sh"
expect_fail "extra file" sh scripts/publish-agent.sh publish 9.9.10 "$T/b10"
if [ ! -e "$DIST/agent/v9.9.10" ]; then
	ok "no public dir with extra file present"
else
	fail "public dir with extra file present"
fi

# 10. foreign bundle: bytes signed by another key (e.g. staged by a
# compromised server) do not verify against the release key
minisign -G -W -p "$T/keys/other.pub" -s "$T/keys/other.key" >/dev/null 2>&1
mksigned 9.9.11 "$T/b11"
minisign -S -s "$T/keys/other.key" -m "$T/b11/cadence-agent-linux-amd64" \
	-t "forged" >/dev/null 2>&1
expect_fail "foreign signature" sh scripts/publish-agent.sh publish 9.9.11 "$T/b11"
if [ ! -e "$DIST/agent/v9.9.11" ]; then
	ok "no public dir for foreign-signed bytes"
else
	fail "public dir for foreign-signed bytes"
fi

# 11. validly signed but wrong-version binary: -version binds bytes to the
# published version independently of the file name
mksigned 9.9.12 "$T/b12"
mkstub "9.9.99" "$T/b12/cadence-agent-linux-amd64"
(cd "$T/b12" && sha256sum cadence-agent-linux-amd64 >cadence-agent-linux-amd64.sha256)
minisign -S -s "$KEY" -m "$T/b12/cadence-agent-linux-amd64" \
	-t "test" >/dev/null 2>&1
expect_fail "wrong -version" sh scripts/publish-agent.sh publish 9.9.12 "$T/b12"
if [ ! -e "$DIST/agent/v9.9.12" ]; then
	ok "no public dir for mislabeled bytes"
else
	fail "public dir for mislabeled bytes"
fi

# 12. swapped architectures: the ELF machine check catches them
mksigned 9.9.13 "$T/b13"
mv "$T/b13/cadence-agent-linux-amd64" "$T/b13/swp"
mv "$T/b13/cadence-agent-linux-arm64" "$T/b13/cadence-agent-linux-amd64"
mv "$T/b13/swp" "$T/b13/cadence-agent-linux-arm64"
(cd "$T/b13" \
	&& sha256sum cadence-agent-linux-amd64 >cadence-agent-linux-amd64.sha256 \
	&& sha256sum cadence-agent-linux-arm64 >cadence-agent-linux-arm64.sha256)
minisign -S -s "$KEY" -m "$T/b13/cadence-agent-linux-amd64" \
	-t "test" >/dev/null 2>&1
minisign -S -s "$KEY" -m "$T/b13/cadence-agent-linux-arm64" \
	-t "test" >/dev/null 2>&1
expect_fail "swapped arch" sh scripts/publish-agent.sh publish 9.9.13 "$T/b13"
if [ ! -e "$DIST/agent/v9.9.13" ]; then
	ok "no public dir for swapped architectures"
else
	fail "public dir for swapped architectures"
fi

# --- part 1b: 2-key trust set -------------------------------------------

# 14. the backup key alone verifies (signed with KEY_BACKUP, not KEY):
# proves the loop genuinely tries the second key, not just the first.
mksigned 9.9.14 "$T/b14"
rm "$T/b14"/*.minisig
minisign -S -s "$KEY_BACKUP" -m "$T/b14/cadence-agent-linux-amd64" \
	-t "test 9.9.14" >/dev/null 2>&1
minisign -S -s "$KEY_BACKUP" -m "$T/b14/cadence-agent-linux-arm64" \
	-t "test 9.9.14" >/dev/null 2>&1
out=$(sh scripts/publish-agent.sh publish 9.9.14 "$T/b14" 2>&1) && res=0 || res=$?
if [ "$res" -eq 0 ] && [ -e "$DIST/agent/v9.9.14" ]; then
	ok "backup key alone verifies and publishes"
else
	fail "backup key alone did not verify (exit=$res): $out"
fi
backup_id=$(sed -n '1p' "$PUB_BACKUP" | sed 's/^.*key //')
if printf '%s' "$out" | grep -qF "key $backup_id"; then
	ok "publish logs the backup key ID that verified"
else
	fail "publish did not log the verifying key ID"
fi
if printf '%s' "$out" | grep -q "trusted comment: test 9.9.14"; then
	ok "publish logs the trusted comment"
else
	fail "publish did not log the trusted comment"
fi

# 14b. break-glass output as is: a bundle signed by the break-glass signing
# code itself (sign_bundle, the function build-agent-release.sh calls) with
# the backup key is accepted by publish unchanged, the log names the backup
# key, and the trusted comment is the CI format.
# shellcheck source=scripts/agent-sign-lib.sh
. scripts/agent-sign-lib.sh
mksigned 9.9.40 "$T/b40"
rm "$T/b40"/*.minisig
if (trust_set_load "$PWD" 2>/dev/null && trust_set_check && sign_bundle "$KEY_BACKUP" 9.9.40 "$T/b40") >"$T/b40.log" 2>&1; then
	ok "sign_bundle signs a bundle with the backup key and self-verifies"
else
	fail "sign_bundle failed: $(cat "$T/b40.log")"
fi
out=$(sh scripts/publish-agent.sh publish 9.9.40 "$T/b40" 2>&1) && res=0 || res=$?
sum40=$(sha256sum "$T/b40/cadence-agent-linux-amd64" | cut -d' ' -f1)
if [ "$res" -eq 0 ] && [ -e "$DIST/agent/v9.9.40" ] && printf '%s' "$out" | grep -qF "key $backup_id"; then
	ok "publish accepts the break-glass bundle as is and logs the backup key"
else
	fail "publish of the break-glass bundle (exit=$res): $out"
fi
if printf '%s' "$out" | grep -qF "trusted comment: cadence-agent 9.9.40 linux/amd64 sha256:$sum40"; then
	ok "the published signature carries the CI trusted comment format"
else
	fail "unexpected trusted comment: $out"
fi

# 14c. a bundle signed by a key outside the trust set never gets that far:
# sign_bundle refuses it and leaves no signature behind.
mksigned 9.9.41 "$T/b41"
rm "$T/b41"/*.minisig
minisign -G -W -f -p "$T/keys/stranger.pub" -s "$T/keys/stranger.key" >/dev/null 2>&1
if (trust_set_load "$PWD" 2>/dev/null && trust_set_check && sign_bundle "$T/keys/stranger.key" 9.9.41 "$T/b41") >"$T/b41.log" 2>&1; then
	fail "sign_bundle accepted a key outside the trust set"
elif [ ! -e "$T/b41/cadence-agent-linux-amd64.minisig" ] && [ ! -e "$T/b41/cadence-agent-linux-arm64.minisig" ]; then
	ok "a key outside the trust set is refused by the break-glass self-check, no signature left"
else
	fail "signatures left behind by a refused signing"
fi

# 15. active key alone verifies and is tried first (logged key ID matches
# the active key, not the backup, for a bundle only the active key signed
# -- already exercised by every earlier test; assert the log explicitly
# once here).
mksigned 9.9.15 "$T/b15"
active_id=$(sed -n '1p' "$PUB" | sed 's/^.*key //')
out=$(sh scripts/publish-agent.sh publish 9.9.15 "$T/b15" 2>&1) && res=0 || res=$?
if [ "$res" -eq 0 ] && printf '%s' "$out" | grep -qF "key $active_id"; then
	ok "active key alone verifies and is logged"
else
	fail "active key path did not log as expected (exit=$res): $out"
fi

# 16. neither key verifies (foreign signature): both are tried, both fail,
# publish refuses. Reuses the same bundle shape as the existing foreign-
# signature case (test 10) but asserted here as the 2-key end state.
minisign -G -W -p "$T/keys/other2.pub" -s "$T/keys/other2.key" >/dev/null 2>&1
mksigned 9.9.16 "$T/b16"
minisign -S -s "$T/keys/other2.key" -m "$T/b16/cadence-agent-linux-amd64" \
	-t "forged" >/dev/null 2>&1
expect_fail "neither key verifies" sh scripts/publish-agent.sh publish 9.9.16 "$T/b16"
if [ ! -e "$DIST/agent/v9.9.16" ]; then
	ok "no public dir when neither trusted key verifies"
else
	fail "public dir when neither trusted key verifies"
fi

# 17. active and backup public keys share a key ID: refuse to start, no
# bundle inspection at all. Scoped to this one invocation only.
mksigned 9.9.17 "$T/b17"
CADENCE_PUBLISH_PUBKEY_BACKUP="$PUB" \
	expect_fail "duplicate trust keys" sh scripts/publish-agent.sh publish 9.9.17 "$T/b17"
if [ ! -e "$DIST/agent/v9.9.17" ]; then
	ok "no public dir when active and backup keys are identical"
else
	fail "public dir when active and backup keys are identical"
fi

# 18. backup public key absent: refuse to start
mksigned 9.9.18 "$T/b18"
CADENCE_PUBLISH_PUBKEY_BACKUP="$T/keys/absent-backup.pub" \
	expect_fail "backup pubkey absent" sh scripts/publish-agent.sh publish 9.9.18 "$T/b18"
if [ ! -e "$DIST/agent/v9.9.18" ]; then
	ok "no public dir when the backup public key is absent"
else
	fail "public dir when the backup public key is absent"
fi

# 19. backup public key malformed: refuse to start
mksigned 9.9.19 "$T/b19"
printf 'not a minisign key\nat all\n' >"$T/keys/garbage-backup.pub"
CADENCE_PUBLISH_PUBKEY_BACKUP="$T/keys/garbage-backup.pub" \
	expect_fail "backup pubkey malformed" sh scripts/publish-agent.sh publish 9.9.19 "$T/b19"
if [ ! -e "$DIST/agent/v9.9.19" ]; then
	ok "no public dir when the backup public key is malformed"
else
	fail "public dir when the backup public key is malformed"
fi

# 20. minisign not installed: clear message naming the package, nonzero
# exit, no fallback to an unsigned publish. Built from a restricted PATH
# holding only the coreutils this script and the test itself need, minus
# minisign, rather than guessing which directory the real binary lives in
# (fragile on a distro where every package shares one bin directory).
NO_MINISIGN_BIN="$T/no-minisign-bin"
mkdir -p "$NO_MINISIGN_BIN"
for tool in dirname pwd find wc tr awk sha256sum od sed cat mkdir rm cp mv \
	uname grep cut basename printf true false; do
	p=$(command -v "$tool" 2>/dev/null) || continue
	ln -sf "$p" "$NO_MINISIGN_BIN/$tool"
done
real_sh=$(command -v sh)
mksigned 9.9.20 "$T/b20"
out=$(PATH="$NO_MINISIGN_BIN" "$real_sh" scripts/publish-agent.sh publish 9.9.20 "$T/b20" 2>&1) \
	&& res=0 || res=$?
if [ "$res" -ne 0 ] && printf '%s' "$out" | grep -qi "minisign"; then
	ok "missing minisign: clear message and nonzero exit"
else
	fail "missing minisign: unclear behavior (exit=$res): $out"
fi
if [ ! -e "$DIST/agent/v9.9.20" ]; then
	ok "no public dir when minisign is missing"
else
	fail "public dir when minisign is missing"
fi

# --- part 1c: fetch, against a throwaway local HTTPS server -------------
# No real network to GitHub. A real curl client against a real TLS
# listener is trusted the standard curl way (CURL_CA_BUNDLE naming our
# throwaway CA), never through a test-only flag added to publish-agent.sh.
# python3 and openssl are the only extra tools this part needs.

# mkrelease_assets <version> <destdir> <signing-key>: the 6 release assets
# fetch expects (binary+sha256+minisig per arch), signed with the given
# key. Same bundle shape as mksigned, minus the local "version" marker:
# fetch writes that itself, a real GitHub Release has no such file.
mkrelease_assets() {
	ver=$1
	dir=$2
	signkey=$3
	mkdir -p "$dir"
	mkstub "$ver" "$dir/cadence-agent-linux-amd64"
	cp "$dir/cadence-agent-linux-amd64" "$dir/cadence-agent-linux-arm64"
	printf '\267\000' | dd of="$dir/cadence-agent-linux-arm64" bs=1 seek=18 \
		conv=notrunc 2>/dev/null
	(
		cd "$dir"
		sha256sum cadence-agent-linux-amd64 >cadence-agent-linux-amd64.sha256
		sha256sum cadence-agent-linux-arm64 >cadence-agent-linux-arm64.sha256
	)
	minisign -S -s "$signkey" -m "$dir/cadence-agent-linux-amd64" \
		-t "test $ver" >/dev/null 2>&1
	minisign -S -s "$signkey" -m "$dir/cadence-agent-linux-arm64" \
		-t "test $ver" >/dev/null 2>&1
}

if ! command -v python3 >/dev/null 2>&1 || ! command -v openssl >/dev/null 2>&1; then
	echo "publish-test: skip fetch tests (need python3 and openssl)"
else
	RELPORT=18443
	REPO_SLUG="test/repo"
	RELROOT="$T/relroot"
	ASSETDIR="$RELROOT/$REPO_SLUG/releases/download"
	mkdir -p "$ASSETDIR" "$T/tls"

	openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
		-keyout "$T/tls/key.pem" -out "$T/tls/cert.pem" \
		-subj '/CN=127.0.0.1' >/dev/null 2>&1

	# Nominal release (9.9.21), one with a missing asset (9.9.22, no
	# .minisig), one signed by a foreign key (9.9.23). Laid out as a real
	# GitHub Release would serve them: releases/download/agent-v<ver>/<name>.
	mkrelease_assets 9.9.21 "$ASSETDIR/agent-v9.9.21" "$KEY"
	mkrelease_assets 9.9.22 "$ASSETDIR/agent-v9.9.22" "$KEY"
	rm "$ASSETDIR/agent-v9.9.22/cadence-agent-linux-amd64.minisig"
	minisign -G -W -p "$T/keys/other3.pub" -s "$T/keys/other3.key" >/dev/null 2>&1
	mkrelease_assets 9.9.23 "$ASSETDIR/agent-v9.9.23" "$T/keys/other3.key"

	python3 -c '
import http.server, ssl, sys
root, port, cert, key = sys.argv[1:5]
def handler(*a, **kw):
	return http.server.SimpleHTTPRequestHandler(*a, directory=root, **kw)
httpd = http.server.HTTPServer(("127.0.0.1", int(port)), handler)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(cert, key)
httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
httpd.serve_forever()
' "$RELROOT" "$RELPORT" "$T/tls/cert.pem" "$T/tls/key.pem" >/dev/null 2>&1 &
	RELPID=$!
	# `|| true`: under `set -e`, a trap that kills an already-dead process
	# (the explicit kill below, or the server having exited on its own)
	# would otherwise fail and that failure would become this whole
	# script's exit status, silently turning "109 passed, 0 failed" into
	# a failed job.
	trap 'kill "$RELPID" 2>/dev/null || true; rm -rf "$T" "$S"' EXIT INT TERM

	export CADENCE_FETCH_REPO="$REPO_SLUG"
	export CADENCE_FETCH_BASE_URL="https://127.0.0.1:$RELPORT"
	export CURL_CA_BUNDLE="$T/tls/cert.pem"
	# Confines fetch's own download temp dir (mktemp -d
	# "${TMPDIR:-/tmp}/publish-agent-fetch.XXXXXX") under $T, so the
	# "no download temp dir left behind" checks below actually look
	# where one would land instead of vacuously passing by checking the
	# wrong directory.
	export TMPDIR="$T"

	up=0
	i=0
	while [ "$i" -lt 20 ]; do
		if curl -fsS "https://127.0.0.1:$RELPORT/" >/dev/null 2>&1; then
			up=1
			break
		fi
		i=$((i + 1))
		sleep 1
	done
	if [ "$up" -ne 1 ]; then
		fail "local HTTPS test server did not start"
	fi

	# 21. malformed version: refused before any request. An unreachable
	# base URL proves it: if fetch had tried to connect, it would fail
	# with a connection error instead of this specific grammar message.
	out=$(CADENCE_FETCH_BASE_URL="https://127.0.0.1:1" \
		sh scripts/publish-agent.sh fetch 1.2 2>&1) && res=0 || res=$?
	if [ "$res" -ne 0 ] && printf '%s' "$out" | grep -q "not strict N.N.N"; then
		ok "fetch: malformed version refused before any request"
	else
		fail "fetch: malformed version did not refuse as expected (exit=$res): $out"
	fi

	# 22. already-published version: refused before any download, same
	# unreachable-URL proof. 9.9.1 is already published by test 1 above.
	out=$(CADENCE_FETCH_BASE_URL="https://127.0.0.1:1" \
		sh scripts/publish-agent.sh fetch 9.9.1 2>&1) && res=0 || res=$?
	if [ "$res" -ne 0 ] && printf '%s' "$out" | grep -q "already published"; then
		ok "fetch: already-published version refused before any download"
	else
		fail "fetch: already-published version did not refuse as expected (exit=$res): $out"
	fi

	# 23. missing asset in the release: clean failure, nothing staged.
	expect_fail "fetch: missing release asset" sh scripts/publish-agent.sh fetch 9.9.22
	if [ ! -e "$DIST/agent/v9.9.22" ]; then
		ok "fetch: no public dir when a release asset is missing"
	else
		fail "fetch: public dir when a release asset is missing"
	fi
	if ls "$T"/publish-agent-fetch.* >/dev/null 2>&1; then
		fail "fetch: download temp dir left behind after missing-asset failure"
	else
		ok "fetch: no download temp dir left behind after missing-asset failure"
	fi

	# 24. invalid signature (foreign key): clean failure, nothing staged.
	expect_fail "fetch: invalid signature" sh scripts/publish-agent.sh fetch 9.9.23
	if [ ! -e "$DIST/agent/v9.9.23" ]; then
		ok "fetch: no public dir for a foreign-signed release"
	else
		fail "fetch: public dir for a foreign-signed release"
	fi

	# 25. nominal case: correct staging, identical to a local publish of
	# the same bytes.
	if sh scripts/publish-agent.sh fetch 9.9.21 >/dev/null 2>&1; then
		n=$(find "$DIST/agent/v9.9.21" -type f | wc -l)
		if [ "$n" = "6" ]; then
			ok "fetch: nominal case stages 6 files"
		else
			fail "fetch: nominal case staged $n files, want 6"
		fi
		if cmp -s "$ASSETDIR/agent-v9.9.21/cadence-agent-linux-amd64" \
			"$DIST/agent/v9.9.21/cadence-agent-linux-amd64"; then
			ok "fetch: staged bytes are byte-identical to the release asset"
		else
			fail "fetch: staged bytes differ from the release asset"
		fi
		if cmp -s "$DIST/agent/v9.9.21/cadence-agent-linux-amd64" "$DIST/agent/cadence-agent"; then
			ok "fetch: unversioned triplet refreshed from amd64"
		else
			fail "fetch: unversioned triplet not refreshed"
		fi
	else
		fail "fetch: nominal case failed"
	fi
	if ls "$T"/publish-agent-fetch.* >/dev/null 2>&1; then
		fail "fetch: download temp dir left behind after success"
	else
		ok "fetch: no download temp dir left behind after success"
	fi

	# --- regression: a dist path containing a space -----------------
	# cleanup() used to pass a space-joined list of paths unquoted to
	# `rm -rf`, which word-splits on any space in $dist or $TMPDIR. A
	# witness directory sharing the exact prefix before the space (what
	# the old bug would have deleted instead of the real staging temp
	# dir) must survive both a local publish and a fetch into such a
	# path.
	SPACE_DIST="$T/mes projets/dist"
	mkdir -p "$SPACE_DIST/agent"
	WITNESS="$T/mes"
	mkdir -p "$WITNESS"
	printf 'witness\n' >"$WITNESS/marker"

	mksigned 9.9.26 "$T/b26"
	if CADENCE_PUBLISH_DIST="$SPACE_DIST" \
		sh scripts/publish-agent.sh publish 9.9.26 "$T/b26" >/dev/null 2>&1; then
		ok "publish into a dist path containing a space succeeds"
	else
		fail "publish into a dist path containing a space failed"
	fi
	if [ -e "$SPACE_DIST/agent/v9.9.26/cadence-agent-linux-amd64" ]; then
		ok "publish: staged correctly under the space-containing path"
	else
		fail "publish: nothing staged under the space-containing path"
	fi
	if ls "$SPACE_DIST"/agent/.staging-* >/dev/null 2>&1; then
		fail "publish: staging temp dir left behind under the space-containing path"
	else
		ok "publish: no staging temp dir left behind under the space-containing path"
	fi
	if [ -f "$WITNESS/marker" ]; then
		ok "publish: witness directory (space prefix) untouched"
	else
		fail "publish: witness directory was removed (cleanup word-split bug)"
	fi

	mkrelease_assets 9.9.27 "$ASSETDIR/agent-v9.9.27" "$KEY"
	if CADENCE_PUBLISH_DIST="$SPACE_DIST" \
		sh scripts/publish-agent.sh fetch 9.9.27 >/dev/null 2>&1; then
		ok "fetch into a dist path containing a space succeeds"
	else
		fail "fetch into a dist path containing a space failed"
	fi
	if [ -e "$SPACE_DIST/agent/v9.9.27/cadence-agent-linux-amd64" ]; then
		ok "fetch: staged correctly under the space-containing path"
	else
		fail "fetch: nothing staged under the space-containing path"
	fi
	if ls "$SPACE_DIST"/agent/.staging-* >/dev/null 2>&1; then
		fail "fetch: staging temp dir left behind under the space-containing path"
	else
		ok "fetch: no staging temp dir left behind under the space-containing path"
	fi
	if ls "$T"/publish-agent-fetch.* >/dev/null 2>&1; then
		fail "fetch: download temp dir left behind (space-path case)"
	else
		ok "fetch: no download temp dir left behind (space-path case)"
	fi
	if [ -f "$WITNESS/marker" ]; then
		ok "fetch: witness directory (space prefix) untouched"
	else
		fail "fetch: witness directory was removed (cleanup word-split bug)"
	fi

	# The EXIT/INT/TERM trap above kills $RELPID; no need to do it here
	# too (a redundant kill of an already-dead process would fail under
	# `set -e`, exactly the bug the trap's own `|| true` now guards).
	unset CADENCE_FETCH_REPO CADENCE_FETCH_BASE_URL CURL_CA_BUNDLE TMPDIR
fi

# --- part 1d: CADENCE_FETCH_BASE_URL grammar -----------------------------
# Extracted verbatim from the real script (never retyped), so these cases
# can never silently drift from the implementation under test.
eval "$(sed -n '/^check_fetch_base_url() {/,/^}/p' scripts/publish-agent.sh)"

check_base_url_case() {
	desc=$1
	url=$2
	want=$3
	if check_fetch_base_url "$url"; then got=accept; else got=reject; fi
	if [ "$got" = "$want" ]; then
		ok "base URL $desc -> $want"
	else
		fail "base URL $desc: got $got, want $want"
	fi
}
check_base_url_case "default (https://github.com)" "https://github.com" accept
check_base_url_case "host:port" "https://github.com:8443" accept
check_base_url_case "userinfo (user@host)" "https://user@github.com" reject
check_base_url_case "path" "https://github.com/some/path" reject
check_base_url_case "trailing slash" "https://github.com/" reject
check_base_url_case "query" "https://github.com?x=1" reject
check_base_url_case "fragment" "https://github.com#frag" reject
check_base_url_case "http scheme" "http://github.com" reject
check_base_url_case "no scheme" "github.com" reject
check_base_url_case "empty host" "https://" reject
check_base_url_case "empty port" "https://github.com:" reject
check_base_url_case "non-numeric port" "https://github.com:abc" reject
check_base_url_case "port zero" "https://github.com:0" reject
check_base_url_case "port too big" "https://github.com:70000" reject
check_base_url_case "bad host characters" "https://git_hub.com" reject

# Black-box proof that cmd_fetch actually wires this validator in (not
# just that the function is correct in isolation): a rejected base URL
# refuses before using it for anything, an accepted one is used as-is
# (connection then fails elsewhere, which is a different, later error).
out=$(CADENCE_FETCH_BASE_URL="https://user@127.0.0.1:1" \
	sh scripts/publish-agent.sh fetch 9.9.25 2>&1) && res=0 || res=$?
if [ "$res" -ne 0 ] && printf '%s' "$out" | grep -q "must be exactly https"; then
	ok "fetch: userinfo base URL refused with the grammar message"
else
	fail "fetch: userinfo base URL not refused as expected (exit=$res): $out"
fi

# --- part 1e: .sha256 parser, aligned with the agent's parseSHA256File --
eval "$(sed -n '/^parse_sha256_file() {/,/^}/p' scripts/publish-agent.sh)"

HASH64=$(printf '%064d' 0 | tr '0' 'a')
SHAT=$(mktemp -d)

check_sha_case() {
	desc=$1
	content=$2
	wantname=$3
	want=$4
	printf '%s' "$content" >"$SHAT/case.sha256"
	if parse_sha256_file "$SHAT/case.sha256" "$wantname" >/dev/null 2>&1; then
		got=accept
	else
		got=reject
	fi
	if [ "$got" = "$want" ]; then
		ok ".sha256 parser: $desc -> $want"
	else
		fail ".sha256 parser: $desc: got $got, want $want"
	fi
}
check_sha_case "release.yml format (two spaces, one trailing newline)" \
	"$HASH64  cadence-agent-linux-amd64
" "cadence-agent-linux-amd64" accept
check_sha_case "no trailing newline at all (agent also accepts this)" \
	"$HASH64  cadence-agent-linux-amd64" "cadence-agent-linux-amd64" accept
check_sha_case "path in name" \
	"$HASH64  /abs/path/cadence-agent-linux-amd64
" "cadence-agent-linux-amd64" reject
check_sha_case "single space separator" \
	"$HASH64 cadence-agent-linux-amd64
" "cadence-agent-linux-amd64" reject
# CRLF needs a real \r byte, not expressible as a plain shell literal, so
# it is built explicitly with printf instead of through check_sha_case.
printf '%s  cadence-agent-linux-amd64\r\n' "$HASH64" >"$SHAT/crlf.sha256"
if parse_sha256_file "$SHAT/crlf.sha256" "cadence-agent-linux-amd64" >/dev/null 2>&1; then
	fail ".sha256 parser: CRLF line ending -> accept, want reject"
else
	ok ".sha256 parser: CRLF line ending -> reject"
fi
check_sha_case "two lines" \
	"$HASH64  cadence-agent-linux-amd64
extra
" "cadence-agent-linux-amd64" reject
check_sha_case "wrong arch name" \
	"$HASH64  cadence-agent-linux-arm64
" "cadence-agent-linux-amd64" reject
rm -rf "$SHAT"

# Black-box proof the real publish path uses this stricter parser: a
# single-space .sha256 (accepted by the old lenient awk-based parser) must
# now refuse the whole triplet.
mksigned 9.9.24 "$T/b24"
realhash=$(awk '{print $1}' "$T/b24/cadence-agent-linux-amd64.sha256")
printf '%s cadence-agent-linux-amd64\n' "$realhash" >"$T/b24/cadence-agent-linux-amd64.sha256"
expect_fail "single-space .sha256 (strict parser)" \
	sh scripts/publish-agent.sh publish 9.9.24 "$T/b24"
if [ ! -e "$DIST/agent/v9.9.24" ]; then
	ok "no public dir for a single-space .sha256"
else
	fail "public dir for a single-space .sha256"
fi

# --- part 2: break-glass build + server publish (docker) ----------------
# Simulates both machines: the break-glass release tool builds, smokes,
# hashes and signs with the test BACKUP key (its nominal use), self-checking
# against the trust set from the CADENCE_PUBLISH_PUBKEY* variables exported
# above, then the server path publishes the bundle.
# The server side needs no Go toolchain and no docker here (already
# proven by part 1 running on images without them).
if [ "${DOCKER_TESTS:-0}" = "1" ]; then
	if [ "$(uname -m)" != "x86_64" ]; then
		echo "publish-test: skip docker part (needs linux/amd64 host)"
	else
		rel=$(grep -m1 '^## [0-9][0-9.]*$' agent/CHANGELOG.md | awk '{print $2}')
		if [ -z "$rel" ]; then
			fail "no released version heading in agent/CHANGELOG.md"
		else
			R=$T/rbuild
			mkdir -p "$R/agent"
			cp agent/go.mod agent/go.sum agent/CHANGELOG.md "$R/agent/"
			cp -r agent/cmd agent/internal "$R/agent/"
			(cd "$R" && git init -q 2>/dev/null \
				&& git -c user.email=t@t -c user.name=t add -A \
				&& git -c user.email=t@t -c user.name=t commit -qm src \
				&& git tag "agent-v$rel")
		fi
		if [ -n "$rel" ] && CADENCE_RELEASE_REPO="$R" CADENCE_SIGNING_KEY="$KEY_BACKUP" \
			sh scripts/build-agent-release.sh "$rel" "$T/exp" >"$T/exp.log" 2>&1; then
			ok "break-glass tool builds, smokes and signs with the backup key"
			if grep -qF "verified by key $backup_id (backup)" "$T/exp.log"; then
				ok "the build log names the backup key that verified"
			else
				fail "the build log does not name the verifying key"
			fi
			n=$(find "$T/exp" -maxdepth 1 -type f | wc -l | tr -d ' ')
			if [ "$n" = "7" ]; then
				ok "bundle holds exactly 7 files"
			else
				fail "bundle holds $n files, want 7"
			fi
			if sh scripts/publish-agent.sh publish "$rel" "$T/exp" >/dev/null 2>&1; then
				ok "real bundle publishes end to end"
				if [ -e "$DIST/agent/v$rel/version" ]; then
					fail "version file leaked into served dir"
				else
					ok "served dir holds the triplets only"
				fi
				if [ "$("$DIST/agent/v$rel/cadence-agent-linux-amd64" -version)" = "$rel" ]; then
					ok "staged amd64 binary reports the release"
				else
					fail "staged amd64 binary misreports its version"
				fi
				# The build ran in docker as root: drop its output the same
				# way so the EXIT trap can remove the temp repo.
				docker run --rm -v "$R/agent":/s -w /s golang:1.27 rm -rf bin
			else
				fail "real publish failed"
			fi
		else
			fail "break-glass build failed: $(tail -5 "$T/exp.log" 2>/dev/null)"
		fi
	fi
else
	echo "publish-test: skip docker part (set DOCKER_TESTS=1)"
fi

echo "publish-test: $pass passed, $failed failed"
[ "$failed" -eq 0 ]
