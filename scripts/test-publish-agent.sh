#!/bin/sh
# Prove the versioned, immutable, offline-signed agent publish flow.
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
# modified (overrides: CADENCE_PUBLISH_DIST, CADENCE_PUBLISH_PUBKEY).
#
#   sh scripts/test-publish-agent.sh
#   DOCKER_TESTS=1 sh scripts/test-publish-agent.sh
#
# Requires: minisign installed (apk add / apt-get install minisign).

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
	ok "build-agent-release.sh exists (key-holder build tool)"
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
mkdir -p "$T/keys"
minisign -G -W -p "$PUB" -s "$KEY" >/dev/null 2>&1
export CADENCE_PUBLISH_DIST="$DIST"
export CADENCE_PUBLISH_PUBKEY="$PUB"

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

# --- part 2: key-holder build + server publish (docker) -----------------
# Simulates both machines: the release tool builds, smokes, hashes and
# signs with the test key, then the server path publishes the bundle.
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
			cp agent/go.mod agent/CHANGELOG.md "$R/agent/"
			cp -r agent/cmd agent/internal "$R/agent/"
			(cd "$R" && git init -q 2>/dev/null \
				&& git -c user.email=t@t -c user.name=t add -A \
				&& git -c user.email=t@t -c user.name=t commit -qm src \
				&& git tag "agent-v$rel")
		fi
		if [ -n "$rel" ] && CADENCE_RELEASE_REPO="$R" CADENCE_SIGNING_KEY="$KEY" \
			CADENCE_RELEASE_PUBKEY="$PUB" \
			sh scripts/build-agent-release.sh "$rel" "$T/exp" >/dev/null 2>&1; then
			ok "key-holder tool builds, smokes and signs"
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
			fail "key-holder build failed"
		fi
	fi
else
	echo "publish-test: skip docker part (set DOCKER_TESTS=1)"
fi

echo "publish-test: $pass passed, $failed failed"
[ "$failed" -eq 0 ]
