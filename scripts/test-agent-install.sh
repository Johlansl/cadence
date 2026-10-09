#!/bin/sh
# test-agent-install.sh -- tests for the minisign trust set of agent-install.sh.
#
# Runs the REAL scripts/agent-install.sh as a subprocess, with fake tools
# first in PATH:
#   id       says uid 0 (the script insists on root)
#   curl     serves files from a fixture directory instead of the network
#   dpkg     succeeds, so the apt branch is skipped
#   install  tripwire: records the call and exits 99. The first host change
#            after a successful verification is `install`, so exit 99 means
#            "verification passed"; anything else must never reach it.
#   systemctl, update-ca-certificates, apt-get  exit 98 (must never run)
# Nothing is installed and the network is never touched. Keys are generated
# per run in a temporary directory, never the real ones. Needs minisign.
#
# CADENCE_UPGRADE_ONLY=true is not run here: it needs /etc/cadence/agent.env
# at an absolute path and shares the verification code, which sits before the
# upgrade_only branch of the script.
#
#   sh scripts/test-agent-install.sh

set -eu

HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
INSTALL=$HERE/agent-install.sh

command -v minisign >/dev/null 2>&1 || { echo "install-test: minisign is required" >&2; exit 1; }

pass=0
failed=0

ok() { printf '  ok   %s\n' "$1"; pass=$((pass + 1)); }
fail() { printf '  FAIL %s\n' "$1"; failed=$((failed + 1)); }

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT INT TERM

# --- fake tools -----------------------------------------------------------
BIN=$T/bin
BIN_NOB64=$T/bin-nobase64
mkdir -p "$BIN" "$BIN_NOB64"

cat >"$BIN/id" <<'EOF'
#!/bin/sh
echo 0
EOF
cat >"$BIN/curl" <<'EOF'
#!/bin/sh
out=""
url=""
while [ "$#" -gt 0 ]; do
	case $1 in
	-o) out=$2; shift ;;
	https://*) url=$1 ;;
	esac
	shift
done
echo "$url" >>"$FAKE_DIR/curl.log"
path=${url#https://cadence.test/}
[ -f "$FIX_DIR/$path" ] || exit 22
cp "$FIX_DIR/$path" "$out"
EOF
cat >"$BIN/dpkg" <<'EOF'
#!/bin/sh
exit 0
EOF
cat >"$BIN/install" <<'EOF'
#!/bin/sh
echo "install $*" >>"$FAKE_DIR/install.called"
exit 99
EOF
cat >"$BIN/systemctl" <<'EOF'
#!/bin/sh
echo "$0" >>"$FAKE_DIR/unexpected"
exit 98
EOF
cp "$BIN/systemctl" "$BIN/update-ca-certificates"
cp "$BIN/systemctl" "$BIN/apt-get"
# a base64 that always fails: key ID computation must degrade to "?" only
printf '#!/bin/sh\nexit 1\n' >"$BIN_NOB64/base64"
chmod +x "$BIN"/* "$BIN_NOB64"/*

# --- keys: A (active), B (backup), C (stranger) ------------------------------
mkdir -p "$T/keys"
for k in a b c; do
	minisign -G -W -f -p "$T/keys/$k.pub" -s "$T/keys/$k.key" >/dev/null
done
# Expected IDs come from the comment minisign itself wrote (test oracle only;
# the script under test never trusts that comment).
idof() { sed -n 1p "$T/keys/$1.pub" | awk '{print $NF}'; }
ID_A=$(idof a)
ID_B=$(idof b)
ID_C=$(idof c)
keyof() { sed -n 2p "$T/keys/$1.pub"; }

# --- case runner ----------------------------------------------------------------
UNITS="cadence-agent.service cadence-agent.timer cadence-agent-poll.service
cadence-agent-poll.timer cadence-agent-health-check-boot.service
cadence-agent-health-check-boot.timer"

# new_case <name> <signing key letter> -- fixture + scratch dirs. The binary
# is signed by that key, its .sha256 is the genuine one.
new_case() {
	CASE=$T/case-$1
	FIX_DIR=$CASE/fix
	FAKE_DIR=$CASE/fake
	TD=$CASE/tmp
	mkdir -p "$FIX_DIR/agent/systemd" "$FAKE_DIR" "$TD"
	printf 'agent payload %s\n' "$1" >"$FIX_DIR/agent/cadence-agent"
	(cd "$FIX_DIR/agent" && sha256sum cadence-agent >cadence-agent.sha256)
	minisign -S -s "$T/keys/$2.key" -m "$FIX_DIR/agent/cadence-agent" \
		-x "$FIX_DIR/agent/cadence-agent.minisig" >/dev/null
	for u in $UNITS; do : >"$FIX_DIR/agent/systemd/$u"; done
	: >"$CASE/ca.crt"
	OUT=$CASE/out
}

# run_install <env assignment>... -- runs the real script; sets RC, output in
# $OUT. Extra PATH entries can be prefixed through $EXTRA_PATH.
run_install() {
	RC=0
	env -u CADENCE_MINISIGN_PUB -u CADENCE_MINISIGN_PUB2 -u CADENCE_UPGRADE_ONLY \
		PATH="${EXTRA_PATH:+$EXTRA_PATH:}$BIN:$PATH" TMPDIR="$TD" \
		FAKE_DIR="$FAKE_DIR" FIX_DIR="$FIX_DIR" \
		CADENCE_BASE_URL=https://cadence.test CADENCE_VERIFIED_CA="$CASE/ca.crt" \
		"$@" sh "$INSTALL" >"$OUT" 2>&1 || RC=$?
}

reached_install() { [ -f "$FAKE_DIR/install.called" ]; }
no_leftovers() { [ -z "$(ls -A "$TD")" ]; }
no_unexpected() { [ ! -e "$FAKE_DIR/unexpected" ]; }
downloads() { if [ -f "$FAKE_DIR/curl.log" ]; then wc -l <"$FAKE_DIR/curl.log" | tr -d ' '; else echo 0; fi; }

# expect_pass <label> <expected key id | none> -- verification passed: the
# tripwire was reached (exit 99), the log names the key, temp dir cleaned up.
expect_pass() {
	if [ "$RC" -eq 99 ] && reached_install && no_unexpected && no_leftovers; then
		if [ "$2" = none ]; then
			if grep -q "authenticated download and sha256 OK" "$OUT" && ! grep -q "minisign signature" "$OUT"; then
				ok "$1 (exit=$RC, install reached, no signature step)"
			else
				fail "$1 (exit=$RC): unexpected log: $(cat "$OUT")"
			fi
		elif grep -q "install.sh: minisign signature OK (key $2)" "$OUT"; then
			ok "$1 (exit=$RC, install reached, key $2)"
		else
			fail "$1 (exit=$RC): wrong or missing key id: $(cat "$OUT")"
		fi
	else
		fail "$1 (exit=$RC, want 99 with install reached): $(cat "$OUT")"
	fi
}

# expect_fail <label> <message fragment> -- exit 1, expected message, the
# tripwire NOT reached, temp dir cleaned up.
expect_fail() {
	if [ "$RC" -eq 1 ] && ! reached_install && no_unexpected && no_leftovers &&
		grep -qF -- "$2" "$OUT"; then
		ok "$1 (exit=$RC, install not reached)"
	else
		fail "$1 (exit=$RC, want 1 with '$2', install reached: $(reached_install && echo yes || echo no)): $(cat "$OUT")"
	fi
}

# --- the five asked-for cases, then variants ---------------------------------------
echo "1. signed by A"
new_case 1a a
run_install CADENCE_MINISIGN_PUB="$T/keys/a.pub"
expect_pass "case 1: PUB=A (key file), signed by A" "$ID_A"

new_case 1b a
run_install CADENCE_MINISIGN_PUB="$(keyof a)"
expect_pass "case 1: PUB=A (bare key), signed by A" "$ID_A"

new_case 1c a
run_install CADENCE_MINISIGN_PUB="$T/keys/a.pub" CADENCE_MINISIGN_PUB2="$T/keys/b.pub"
expect_pass "case 1: PUB=A PUB2=B, signed by A (first key wins)" "$ID_A"

echo "2. signed by B"
new_case 2a b
run_install CADENCE_MINISIGN_PUB="$T/keys/a.pub" CADENCE_MINISIGN_PUB2="$T/keys/b.pub"
expect_pass "case 2: PUB=A PUB2=B, signed by B (second key)" "$ID_B"

new_case 2b b
run_install CADENCE_MINISIGN_PUB="$(keyof a)" CADENCE_MINISIGN_PUB2="$(keyof b)"
expect_pass "case 2: bare keys, signed by B (second key)" "$ID_B"

echo "3. signed by B, only A trusted"
new_case 3 b
run_install CADENCE_MINISIGN_PUB="$T/keys/a.pub"
expect_fail "case 3: PUB=A only, signed by B" "minisign signature invalid (tried keys: $ID_A; signature claims key $ID_B, unverified)"

echo "4. signed by a stranger, or altered"
new_case 4a c
run_install CADENCE_MINISIGN_PUB="$T/keys/a.pub" CADENCE_MINISIGN_PUB2="$T/keys/b.pub"
expect_fail "case 4: PUB=A PUB2=B, signed by C" "tried keys: $ID_A, $ID_B; signature claims key $ID_C, unverified"

new_case 4b a
printf 'tampered\n' >"$FIX_DIR/agent/cadence-agent"
(cd "$FIX_DIR/agent" && sha256sum cadence-agent >cadence-agent.sha256) # sha256 still matches
run_install CADENCE_MINISIGN_PUB="$T/keys/a.pub" CADENCE_MINISIGN_PUB2="$T/keys/b.pub"
expect_fail "case 4: altered binary with a matching sha256" "minisign signature invalid"

new_case 4c a
printf 'tampered\n' >"$FIX_DIR/agent/cadence-agent"
run_install CADENCE_MINISIGN_PUB="$T/keys/a.pub"
expect_fail "case 4: altered binary, sha256 mismatch" "checksum mismatch"

echo "5. malformed second key"
new_case 5a a
run_install CADENCE_MINISIGN_PUB="$T/keys/a.pub" CADENCE_MINISIGN_PUB2="RWQnotakey"
expect_fail "case 5: PUB2 malformed although PUB would verify" "CADENCE_MINISIGN_PUB2 must be a minisign public key"
if [ "$(downloads)" -eq 0 ]; then ok "case 5: rejected before any download"; else fail "case 5: $(downloads) download(s) happened"; fi

# --- more shape checks (all fail closed, before any download) ---------------------
shape_case() {
	# shape_case <label> <message fragment> <env assignment>...
	label=$1
	frag=$2
	shift 2
	new_case shape a
	run_install "$@"
	expect_fail "$label" "$frag"
	if [ "$(downloads)" -eq 0 ]; then ok "$label: no download"; else fail "$label: $(downloads) download(s)"; fi
}

shape_case "PUB2 without PUB" "CADENCE_MINISIGN_PUB2 requires CADENCE_MINISIGN_PUB" \
	CADENCE_MINISIGN_PUB2="$T/keys/b.pub"
shape_case "PUB truncated key" "CADENCE_MINISIGN_PUB must be" \
	CADENCE_MINISIGN_PUB="$(keyof a | cut -c1-55)"
shape_case "PUB key one character too long" "CADENCE_MINISIGN_PUB must be" \
	CADENCE_MINISIGN_PUB="$(keyof a)A"
shape_case "PUB key with a bad character" "CADENCE_MINISIGN_PUB must be" \
	CADENCE_MINISIGN_PUB="$(keyof a | sed 's/.$/!/')"
shape_case "PUB key with a lowercase prefix" "CADENCE_MINISIGN_PUB must be" \
	CADENCE_MINISIGN_PUB="rw$(keyof a | cut -c3-)"
shape_case "PUB path that does not exist" "CADENCE_MINISIGN_PUB must be" \
	CADENCE_MINISIGN_PUB="$T/keys/nope.pub"
shape_case "PUB is a directory" "CADENCE_MINISIGN_PUB must be" \
	CADENCE_MINISIGN_PUB="$T/keys"
# the README form: the file CONTENT (two lines) given as the value
shape_case "PUB is the two-line file content" "CADENCE_MINISIGN_PUB must be" \
	CADENCE_MINISIGN_PUB="$(cat "$T/keys/a.pub")"

printf 'untrusted comment: x\n%s\nextra line\n' "$(keyof a)" >"$T/keys/three.pub"
shape_case "key file with a third line" "CADENCE_MINISIGN_PUB must be" \
	CADENCE_MINISIGN_PUB="$T/keys/three.pub"
keyof a >"$T/keys/oneline.pub"
shape_case "key file without a comment line" "CADENCE_MINISIGN_PUB must be" \
	CADENCE_MINISIGN_PUB="$T/keys/oneline.pub"
printf 'untrusted comment: x\nRWQnotakey\n' >"$T/keys/badkey.pub"
shape_case "key file whose line 2 is not a key" "CADENCE_MINISIGN_PUB must be" \
	CADENCE_MINISIGN_PUB="$T/keys/badkey.pub"
: >"$T/keys/empty.pub"
shape_case "empty key file as PUB2" "CADENCE_MINISIGN_PUB2 must be" \
	CADENCE_MINISIGN_PUB="$T/keys/a.pub" CADENCE_MINISIGN_PUB2="$T/keys/empty.pub"

# A key file with CRLF line endings is refused by the shape check, before any
# download. Deliberate: the committed keys are LF, and a CR left on the key
# line would make it a different, invalid key.
printf 'untrusted comment: x\r\n%s\r\n' "$(keyof a)" >"$T/keys/crlf.pub"
shape_case "key file with CRLF line endings" "CADENCE_MINISIGN_PUB must be" \
	CADENCE_MINISIGN_PUB="$T/keys/crlf.pub"

# The real committed keys pass the shape check and yield the right key IDs.
# The fixture is signed by test key A, so verification fails, and the failure
# line must list the IDs of the two committed keys, never a shape error.
REPO=$(CDPATH='' cd -- "$HERE/.." && pwd)
new_case repokeys a
run_install CADENCE_MINISIGN_PUB="$REPO/agent/minisign.pub" \
	CADENCE_MINISIGN_PUB2="$REPO/agent/minisign-backup.pub"
expect_fail "committed keys: shape accepted, IDs computed" \
	"minisign signature invalid (tried keys: C9EDE89E1DEEED37, 2167C9939EC0F4FC;"
if grep -q "must be a minisign public key" "$OUT"; then
	fail "committed keys: rejected by the shape check"
else
	ok "committed keys: not rejected by the shape check"
fi

# --- unchanged behaviour ------------------------------------------------------------
new_case none a
run_install
expect_pass "no key at all: sha256 only, as before" none
if [ "$(downloads)" -eq 8 ] && ! grep -q 'cadence-agent.minisig' "$FAKE_DIR/curl.log"; then
	ok "no key at all: the .minisig is not even fetched"
else
	fail "no key at all: unexpected downloads: $(cat "$FAKE_DIR/curl.log")"
fi

# --- key ID computation can never break the script ----------------------------------
new_case nob64a b
EXTRA_PATH=$BIN_NOB64 run_install CADENCE_MINISIGN_PUB="$T/keys/a.pub" CADENCE_MINISIGN_PUB2="$T/keys/b.pub"
expect_pass "base64 failing: verification still passes, id shown as ?" "?"
unset EXTRA_PATH

new_case nob64b c
EXTRA_PATH=$BIN_NOB64 run_install CADENCE_MINISIGN_PUB="$T/keys/a.pub" CADENCE_MINISIGN_PUB2="$T/keys/b.pub"
expect_fail "base64 failing: failure still reported cleanly" "tried keys: ?, ?; signature claims key ?, unverified"
unset EXTRA_PATH

echo "install-test: $pass passed, $failed failed"
[ "$failed" -eq 0 ]
