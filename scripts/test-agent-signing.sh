#!/bin/sh
# test-agent-signing.sh -- tests for the break-glass signing tooling:
# agent-sign-lib.sh (trusted comment, key IDs, trust set, sign_bundle) and
# build-agent-release.sh (everything up to docker).
#
# Needs minisign and git. No docker, no network, and never a real key: every
# key pair is generated here. The key password of the "backup" key is fed to
# minisign by a wrapper placed first in PATH (minisign has no password
# option), so the scripts under test are the real ones and see a real
# password prompt. The password must never show up in any output.
#
# Not covered here: backup-signing-key.sh and restore-signing-key.sh
# (scripts/test-agent-key-backup.sh), and a real docker build whose output
# publish-agent.sh accepts (scripts/test-publish-agent.sh).
#
#   sh scripts/test-agent-signing.sh

set -eu

HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH='' cd -- "$HERE/.." && pwd)

command -v minisign >/dev/null 2>&1 || { echo "signing-test: minisign is required" >&2; exit 1; }
command -v git >/dev/null 2>&1 || { echo "signing-test: git is required" >&2; exit 1; }
REAL_MINISIGN=$(command -v minisign)
export REAL_MINISIGN

pass=0
failed=0

ok() { printf '  ok   %s\n' "$1"; pass=$((pass + 1)); }
fail() { printf '  FAIL %s\n' "$1"; failed=$((failed + 1)); }

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT INT TERM
export HOME="$T/home"
mkdir -p "$HOME"

# shellcheck source=scripts/agent-sign-lib.sh
. "$HERE/agent-sign-lib.sh"

# --- keys: A active, B backup, C stranger ---------------------------------------
mkdir -p "$T/keys"
for k in a b c; do
	minisign -G -W -f -p "$T/keys/$k.pub" -s "$T/keys/$k.key" >/dev/null
done
# test oracle only: the ID minisign itself wrote in the comment line
idof() { sed -n 1p "$T/keys/$1.pub" | awk '{print $NF}'; }
ID_A=$(idof a)
ID_B=$(idof b)
export CADENCE_PUBLISH_PUBKEY="$T/keys/a.pub"
export CADENCE_PUBLISH_PUBKEY_BACKUP="$T/keys/b.pub"

# wrapper: gives the password to every minisign call (twice, for -C)
PW='correct horse battery test'
mkdir -p "$T/pwbin"
cat >"$T/pwbin/minisign" <<'EOF'
#!/bin/sh
printf '%s\n%s\n' "$TEST_PW" "$TEST_PW" | exec "$REAL_MINISIGN" "$@"
EOF
chmod +x "$T/pwbin/minisign"
# the call sb_pw runs in a fresh sh: <scripts dir> <trust root> <key> <version> <dir>
cat >"$T/signcall.sh" <<'EOF'
. "$1/agent-sign-lib.sh"
trust_set_load "$2" && trust_set_check && sign_bundle "$3" "$4" "$5"
EOF
# with_pw <password> <cmd...>: run a command with the wrapper first in PATH
with_pw() {
	_pw=$1
	shift
	PATH="$T/pwbin:$PATH" TEST_PW=$_pw "$@"
}

# new_dir <name>: two fake binaries; sets D, H_AMD64, H_ARM64
new_dir() {
	D=$T/$1
	mkdir -p "$D"
	printf 'amd64 payload %s\n' "$1" >"$D/cadence-agent-linux-amd64"
	printf 'arm64 payload %s\n' "$1" >"$D/cadence-agent-linux-arm64"
	H_AMD64=$(sha256sum "$D/cadence-agent-linux-amd64" | cut -d' ' -f1)
	H_ARM64=$(sha256sum "$D/cadence-agent-linux-arm64" | cut -d' ' -f1)
}
no_sigs() { [ ! -e "$1/cadence-agent-linux-amd64.minisig" ] && [ ! -e "$1/cadence-agent-linux-arm64.minisig" ]; }

# sb <secret key> <version> <dir>: sign_bundle against the test trust set, in a
# subshell. Sets RC, output in $OUT.
OUT=$T/out
sb() {
	RC=0
	( trust_set_load "$T" && trust_set_check && sign_bundle "$1" "$2" "$3" ) >"$OUT" 2>&1 || RC=$?
}
# sb_pw <password> <secret key> <version> <dir>: same, with the password given
# to minisign through the wrapper (a fresh sh, so no environment leaks around).
sb_pw() {
	_p=$1
	shift
	RC=0
	with_pw "$_p" sh "$T/signcall.sh" "$HERE" "$T" "$@" >"$OUT" 2>&1 || RC=$?
}
# --- 1. the trusted comment: one definition ---------------------------------------------
echo "1. trusted comment"
want="cadence-agent 0.15.0 linux/amd64 sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
got=$(agent_trusted_comment 0.15.0 amd64 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef)
if [ "$got" = "$want" ]; then ok "format is exactly 'cadence-agent <version> linux/<arch> sha256:<hash>'"; else fail "format is '$got'"; fi

# the CI script and the break-glass signing must produce byte-identical signatures
new_dir eq1
mkdir "$T/eq2"
cp "$D/cadence-agent-linux-amd64" "$D/cadence-agent-linux-arm64" "$T/eq2/"
EQ1=$D
RC=0
sh "$HERE/ci-sign-agent.sh" 0.15.0 "$T/keys/a.pub" "$T/keys/a.key" "$EQ1" \
	"amd64=$H_AMD64" "arm64=$H_ARM64" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -eq 0 ]; then ok "ci-sign-agent.sh signs the fixture (exit=$RC)"; else fail "ci-sign-agent.sh exit=$RC: $(cat "$OUT")"; fi
sb "$T/keys/a.key" 0.15.0 "$T/eq2"
if [ "$RC" -eq 0 ]; then ok "sign_bundle signs the same fixture (exit=$RC)"; else fail "sign_bundle exit=$RC: $(cat "$OUT")"; fi
for a in amd64 arm64; do
	if cmp -s "$EQ1/cadence-agent-linux-$a.minisig" "$T/eq2/cadence-agent-linux-$a.minisig"; then
		ok "$a: CI signature and break-glass signature are byte-identical"
	else
		fail "$a: CI and break-glass signatures differ"
	fi
	h=$H_AMD64
	[ "$a" = arm64 ] && h=$H_ARM64
	if [ "$(sed -n 3p "$T/eq2/cadence-agent-linux-$a.minisig")" = "trusted comment: cadence-agent 0.15.0 linux/$a sha256:$h" ]; then
		ok "$a: trusted comment line is the exact expected text"
	else
		fail "$a: trusted comment line differs"
	fi
done

# --- 2. key IDs ------------------------------------------------------------------------
echo "2. key IDs"
for k in a b c; do
	want=$(idof "$k")
	got=$(pub_file_id "$T/keys/$k.pub")
	if [ "$got" = "$want" ]; then ok "key_id of test key $k is $want"; else fail "key_id of $k is '$got', want $want"; fi
done
if [ "$(pub_file_id "$REPO/agent/minisign.pub")" = "C9EDE89E1DEEED37" ]; then ok "committed active key: C9EDE89E1DEEED37"; else fail "committed active key id: $(pub_file_id "$REPO/agent/minisign.pub")"; fi
if [ "$(pub_file_id "$REPO/agent/minisign-backup.pub")" = "2167C9939EC0F4FC" ]; then ok "committed backup key: 2167C9939EC0F4FC"; else fail "committed backup key id: $(pub_file_id "$REPO/agent/minisign-backup.pub")"; fi
if [ "$(key_id 'not base64 !!')" = "?" ]; then ok "key_id of garbage is ?, and does not fail"; else fail "key_id of garbage"; fi

# --- 3. trust set checks -----------------------------------------------------------------
echo "3. trust set"
( trust_set_load "$REPO" 2>/dev/null; trust_set_check ) >"$OUT" 2>&1 && RC=0 || RC=$?
if [ "$RC" -eq 0 ]; then ok "the committed trust set passes"; else fail "committed trust set refused: $(cat "$OUT")"; fi

( CADENCE_PUBLISH_PUBKEY=$T/keys/a.pub CADENCE_PUBLISH_PUBKEY_BACKUP=$T/keys/a.pub
	trust_set_load "$REPO" 2>/dev/null; trust_set_check ) >"$OUT" 2>&1 && RC=0 || RC=$?
if [ "$RC" -ne 0 ] && grep -q "same key" "$OUT"; then ok "the same key twice is refused"; else fail "duplicate key accepted (rc=$RC)"; fi

printf 'untrusted comment: x\r\n%s\r\n' "$(sed -n 2p "$T/keys/a.pub")" >"$T/keys/crlf.pub"
( CADENCE_PUBLISH_PUBKEY=$T/keys/crlf.pub trust_set_load "$REPO" 2>/dev/null; trust_set_check ) >"$OUT" 2>&1 && RC=0 || RC=$?
if [ "$RC" -ne 0 ]; then ok "a CRLF key file is refused"; else fail "CRLF key accepted"; fi

( CADENCE_PUBLISH_PUBKEY=$T/keys/absent.pub trust_set_load "$REPO" 2>/dev/null; trust_set_check ) >"$OUT" 2>&1 && RC=0 || RC=$?
if [ "$RC" -ne 0 ]; then ok "an absent key file is refused"; else fail "absent key accepted"; fi

( CADENCE_PUBLISH_PUBKEY=$T/keys/a.pub trust_set_load "$REPO" ) >"$OUT" 2>&1
if grep -q "WARNING: trust set override" "$OUT"; then ok "an override prints a warning on stderr"; else fail "no override warning"; fi

# --- 4. sign_bundle ----------------------------------------------------------------------
echo "4. sign_bundle (break-glass signing)"
new_dir sb1
sb "$T/keys/b.key" 0.15.0 "$D"
if [ "$RC" -eq 0 ] && grep -q "verified by key $ID_B (backup)" "$OUT"; then
	ok "signed by the backup key: verified, log names key $ID_B (backup) (exit=$RC)"
else
	fail "backup-signed bundle (exit=$RC): $(cat "$OUT")"
fi
if minisign -V -p "$T/keys/b.pub" -x "$D/cadence-agent-linux-arm64.minisig" -m "$D/cadence-agent-linux-arm64" -q; then
	ok "the signature verifies independently with the backup public key"
else
	fail "independent verification failed"
fi

new_dir sb2
sb "$T/keys/a.key" 0.15.0 "$D"
if [ "$RC" -eq 0 ] && grep -q "verified by key $ID_A (active)" "$OUT"; then
	ok "signed by the active key: log names key $ID_A (active) (exit=$RC)"
else
	fail "active-signed bundle (exit=$RC): $(cat "$OUT")"
fi

new_dir sb3
sb "$T/keys/c.key" 0.15.0 "$D"
if [ "$RC" -ne 0 ] && no_sigs "$D" && grep -q "does not verify against the trust set" "$OUT"; then
	ok "unknown key refused by the self-check, no signature left (exit=$RC)"
else
	fail "unknown key (exit=$RC): $(cat "$OUT")"
fi

# a password-protected backup key, as in real life
cp "$T/keys/b.key" "$T/keys/b-enc.key"
with_pw "$PW" minisign -C -s "$T/keys/b-enc.key" >/dev/null 2>&1
if key_file_encrypted "$T/keys/b-enc.key"; then ok "test setup: the backup key is now password protected"; else fail "test setup: key not encrypted"; fi
if key_file_encrypted "$T/keys/b.key"; then fail "key_file_encrypted says a plain key is encrypted"; else ok "key_file_encrypted: a plain key is not encrypted"; fi

new_dir sb4
sb_pw "$PW" "$T/keys/b-enc.key" 0.15.0 "$D"
if [ "$RC" -eq 0 ] && grep -q "verified by key $ID_B (backup)" "$OUT"; then
	ok "password-protected backup key signs and self-verifies (exit=$RC)"
else
	fail "protected key (exit=$RC): $(cat "$OUT")"
fi
if grep -qF "$PW" "$OUT"; then fail "the password appears in the output"; else ok "the password does not appear in the output"; fi

new_dir sb5
sb_pw "wrong" "$T/keys/b-enc.key" 0.15.0 "$D"
if [ "$RC" -ne 0 ] && no_sigs "$D"; then ok "wrong password: refused, no signature left (exit=$RC)"; else fail "wrong password (exit=$RC)"; fi

# --- 5. build-agent-release.sh up to the docker step -----------------------------------
echo "5. build-agent-release.sh (no docker)"
BUILD=$HERE/build-agent-release.sh
RC=0
RELREPO=$T/rel
mkdir -p "$RELREPO/agent"
printf '## 0.15.0\n' >"$RELREPO/agent/CHANGELOG.md"
(cd "$RELREPO" && git init -q && git add -A &&
	git -c user.email=t@t -c user.name=t commit -qm init && git tag agent-v0.15.0) >/dev/null 2>&1
mkdir -p "$T/fakebin"
printf '#!/bin/sh\necho "$*" >>"%s/docker.called"\nexit 99\n' "$T" >"$T/fakebin/docker"
chmod +x "$T/fakebin/docker"

RC=0
CADENCE_RELEASE_PUBKEY="$T/keys/a.pub" CADENCE_SIGNING_KEY="$T/keys/b.key" \
	sh "$BUILD" 0.15.0 "$T/o1" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -eq 2 ] && grep -q "CADENCE_RELEASE_PUBKEY no longer exists" "$OUT"; then
	ok "CADENCE_RELEASE_PUBKEY is refused loudly, not ignored (exit=$RC)"
else
	fail "CADENCE_RELEASE_PUBKEY (exit=$RC): $(cat "$OUT")"
fi

RC=0
env -u CADENCE_SIGNING_KEY sh "$BUILD" 0.15.0 "$T/o2" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -eq 2 ] && grep -q "CADENCE_SIGNING_KEY is required" "$OUT"; then
	ok "no default signing key: CADENCE_SIGNING_KEY is required (exit=$RC)"
else
	fail "missing CADENCE_SIGNING_KEY (exit=$RC): $(cat "$OUT")"
fi

if [ "$(uname -s)" = "Linux" ] && [ "$(uname -m)" = "x86_64" ]; then
	RC=0
	PATH="$T/fakebin:$PATH" CADENCE_RELEASE_REPO="$RELREPO" CADENCE_SIGNING_KEY="$T/keys/absent.key" \
		sh "$BUILD" 0.15.0 "$T/o3" >"$OUT" 2>&1 || RC=$?
	if [ "$RC" -eq 1 ] && grep -q "no signing key at" "$OUT" && [ ! -e "$T/docker.called" ]; then
		ok "absent key file: refused before any build (exit=$RC)"
	else
		fail "absent key file (exit=$RC): $(cat "$OUT")"
	fi

	RC=0
	PATH="$T/fakebin:$PATH" CADENCE_RELEASE_REPO="$RELREPO" CADENCE_SIGNING_KEY="$T/keys/b.key" \
		CADENCE_PUBLISH_PUBKEY="$T/keys/crlf.pub" sh "$BUILD" 0.15.0 "$T/o4" >"$OUT" 2>&1 || RC=$?
	if [ "$RC" -eq 1 ] && grep -q "trust set:" "$OUT" && [ ! -e "$T/docker.called" ]; then
		ok "malformed trust set: refused before any build (exit=$RC)"
	else
		fail "malformed trust set (exit=$RC): $(cat "$OUT")"
	fi

	RC=0
	PATH="$T/fakebin:$PATH" CADENCE_RELEASE_REPO="$RELREPO" CADENCE_SIGNING_KEY="$T/keys/b.key" \
		sh "$BUILD" 0.15.0 "$T/o5" >"$OUT" 2>&1 || RC=$?
	if [ "$RC" -ne 0 ] && [ -e "$T/docker.called" ] && grep -q "WARNING: trust set override" "$OUT"; then
		ok "valid trust set and key: the build step is reached (fake docker stops it, exit=$RC)"
	else
		fail "valid configuration (exit=$RC): $(cat "$OUT")"
	fi
	if [ ! -e "$T/o5" ] && [ -z "$(ls -d "$T"/o5.tmp.* 2>/dev/null)" ]; then
		ok "a failed build leaves no output or temp directory"
	else
		fail "leftovers after a failed build"
	fi
else
	echo "  skip build-agent-release.sh cases that need linux/x86_64"
fi

# --- 6. the sign job of release.yml has every file ci-sign-agent.sh needs -------------------
# ci-sign-agent.sh sources scripts/agent-sign-lib.sh and
# scripts/agent-version-lib.sh from its own directory. The
# `sign` job runs `sh scripts/ci-sign-agent.sh` from the workspace root, so the
# job must check the whole tree out there. These static checks fail if a later
# edit adds a sparse checkout, a checkout path, a working-directory or a
# private-only file to that picture.
echo "6. release.yml sign job"
WF=$REPO/.github/workflows/release.yml

# sourced_files <script>: files a script sources next to itself, as repo paths
sourced_files() { sed -n 's|^\. "[$]here/\([^"]*\)"$|scripts/\1|p' "$1"; }
# sign_job <workflow>: the text of the `sign` job
sign_job() { awk '/^  sign:$/ {p = 1; next} p && /^  [a-z_-]+:$/ {p = 0} p' "$1"; }
# checkout_step: stdin is a job; prints its actions/checkout step
checkout_step() { awk '/^      - uses: actions\/checkout@/ {p = 1; print; next} p && /^      - / {p = 0} p'; }

# sign_job_gets <workflow> <repo path>...: succeed when the sign job checks the
# whole tree out at the workspace root and runs scripts/ci-sign-agent.sh from
# there, so every listed file sits where the script expects it. A sparse
# checkout is accepted only if it lists every file. Prints the reason if not.
sign_job_gets() {
	_wf=$1
	shift
	_job=$(sign_job "$_wf")
	if [ -z "$_job" ]; then echo "no sign job"; return 1; fi
	_co=$(printf '%s\n' "$_job" | checkout_step)
	if [ -z "$_co" ]; then echo "the sign job has no actions/checkout step"; return 1; fi
	if printf '%s\n' "$_co" | grep -qE '^[[:space:]]+(path|repository|ref):'; then
		echo "the checkout step moves the tree (path, repository or ref)"
		return 1
	fi
	if printf '%s\n' "$_job" | grep -qE 'working-directory:|^[[:space:]]*cd[[:space:]]'; then
		echo "the sign job changes directory before running the script"
		return 1
	fi
	if ! printf '%s\n' "$_job" | grep -qF 'sh scripts/ci-sign-agent.sh'; then
		echo "the sign job does not run scripts/ci-sign-agent.sh from the workspace root"
		return 1
	fi
	if printf '%s\n' "$_co" | grep -q 'sparse-checkout:'; then
		for _f in "$@"; do
			if ! printf '%s\n' "$_co" | grep -qF "$_f"; then echo "sparse-checkout does not list $_f"; return 1; fi
		done
	fi
}

sourced=$(sourced_files "$HERE/ci-sign-agent.sh")
if [ "$sourced" = "scripts/agent-sign-lib.sh
scripts/agent-version-lib.sh" ]; then
	ok "ci-sign-agent.sh sources exactly scripts/agent-sign-lib.sh and scripts/agent-version-lib.sh (a new sourced file must be added here on purpose)"
else
	fail "ci-sign-agent.sh sources: '$sourced'"
fi
# shellcheck disable=SC2086 # repo paths, no spaces: split on purpose
if sign_job_gets "$WF" scripts/ci-sign-agent.sh $sourced agent/minisign.pub >"$OUT" 2>&1; then
	ok "the real sign job checks the whole tree out at the root and runs the script from there"
else
	fail "real sign job: $(cat "$OUT")"
fi
for f in scripts/ci-sign-agent.sh $sourced agent/minisign.pub; do
	if [ -f "$REPO/$f" ] && ! git -C "$REPO" check-ignore -q -- "$f" 2>/dev/null; then
		ok "$f exists in the tree and is not gitignored (it will be in the checkout)"
	else
		fail "$f is missing or gitignored"
	fi
done

# the checker itself, on synthetic workflows
mkwf() { # mkwf <name> <checkout extra lines> <run step extra>
	cat >"$T/$1.yml" <<EOF
jobs:
  build:
    steps:
      - uses: actions/checkout@abc
  sign:
    needs: build
    steps:
      # comment
      - uses: actions/checkout@abc
        with:
          persist-credentials: false
$2
      - name: sign
        run: |
$3          sh scripts/ci-sign-agent.sh 0.15.0 agent/minisign.pub key dir
  publish:
    steps:
      - run: echo done
EOF
}
mkwf good '' ''
mkwf sparse-missing '          sparse-checkout: |
            scripts/ci-sign-agent.sh
            agent/minisign.pub' ''
mkwf sparse-full '          sparse-checkout: |
            scripts/ci-sign-agent.sh
            scripts/agent-sign-lib.sh
            agent/minisign.pub' ''
mkwf moved '          path: repo' ''
mkwf cd '' '          cd somewhere
'
FILES="scripts/ci-sign-agent.sh scripts/agent-sign-lib.sh agent/minisign.pub"
# shellcheck disable=SC2086
if sign_job_gets "$T/good.yml" $FILES >"$OUT" 2>&1; then ok "checker: a plain full checkout is accepted"; else fail "checker rejects the good workflow: $(cat "$OUT")"; fi
# shellcheck disable=SC2086
if sign_job_gets "$T/sparse-full.yml" $FILES >"$OUT" 2>&1; then ok "checker: a sparse checkout listing every file is accepted"; else fail "checker rejects the full sparse list: $(cat "$OUT")"; fi
# shellcheck disable=SC2086
if sign_job_gets "$T/sparse-missing.yml" $FILES >"$OUT" 2>&1; then
	fail "checker accepted a sparse checkout without the library"
elif grep -q "does not list scripts/agent-sign-lib.sh" "$OUT"; then
	ok "checker: a sparse checkout without the library is refused, naming it"
else
	fail "wrong refusal for the sparse list: $(cat "$OUT")"
fi
# shellcheck disable=SC2086
if sign_job_gets "$T/moved.yml" $FILES >"$OUT" 2>&1; then fail "checker accepted a checkout path"; else ok "checker: a checkout into a subdirectory is refused"; fi
# shellcheck disable=SC2086
if sign_job_gets "$T/cd.yml" $FILES >"$OUT" 2>&1; then fail "checker accepted a cd in the sign job"; else ok "checker: a cd before the script is refused"; fi
printf 'jobs:\n  build:\n    steps:\n      - run: true\n' >"$T/nosign.yml"
# shellcheck disable=SC2086
if sign_job_gets "$T/nosign.yml" $FILES >"$OUT" 2>&1; then fail "checker accepted a workflow without a sign job"; else ok "checker: a workflow without a sign job is refused"; fi

echo "signing-test: $pass passed, $failed failed"
[ "$failed" -eq 0 ]
