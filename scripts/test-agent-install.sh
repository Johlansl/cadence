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
# CADENCE_UPGRADE_ONLY=true (section 6, the downgrade check) needs absolute
# paths: /etc/cadence/agent.env and the two agent binaries. It runs a COPY of
# the script in which those paths, and the 30 second probe timeout, are
# rewritten to fixture values. Every rewrite is counted against the real
# script, so a change to one of those literals fails the test instead of
# silently testing something else. The production script has no override.
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
		"$@" sh "${INSTALL_UNDER_TEST:-$INSTALL}" >"$OUT" 2>&1 || RC=$?
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

# --- 6. CADENCE_UPGRADE_ONLY=true: the downgrade check ------------------------------
# Fixture agents are tiny shell scripts that print a chosen -version output and
# append one line to a trace file when they run: which binary ran, and whether
# the CADENCE_PROBE_CANARY variable of the installer's environment reached it
# (it must not: every probe runs with an empty environment).
UPN=0

# need_lines <pattern> <count>: the real script must have exactly <count>
# lines matching <pattern>, or this test no longer tests what it claims.
need_lines() {
	_got=$(grep -c -- "$1" "$INSTALL" || true)
	if [ "$_got" != "$2" ]; then
		echo "install-test: $INSTALL has $_got line(s) matching '$1', the test expects $2: update the test" >&2
		exit 1
	fi
}

# make_up_copy <fixture root>: the script under test, paths rewritten.
make_up_copy() {
	need_lines '/etc/cadence/agent\.env' 2
	need_lines '/usr/bin/cadence-agent' 4
	need_lines '/usr/local/bin/cadence-agent' 1
	need_lines 'timeout -s KILL 30 ' 1
	sed -e "s|/etc/cadence/agent\.env|$1/etc/cadence/agent.env|g" \
		-e "s|/usr/bin/cadence-agent|$1/usr/bin/cadence-agent|g" \
		-e "s|/usr/local/bin/cadence-agent|$1/usr/local/bin/cadence-agent|g" \
		-e "s|timeout -s KILL 30 |timeout -s KILL 1 |g" "$INSTALL" >"$CASE/install-under-test.sh"
}

# mk_agent <path> <spec>: fixture agent. Specs: v:<text> prints <text>; empty;
# nonl (version without newline); two (two lines); exit3 (version, status 3);
# big (6000 bytes); hang (sleeps past the timeout).
mk_agent() {
	mkdir -p "$(dirname "$1")"
	{
		printf '#!/bin/sh\n'
		# the expansions are meant for the fixture agent, not for this shell
		# shellcheck disable=SC2016
		printf 'echo "$0 canary=${CADENCE_PROBE_CANARY:-absent}" >>"%s"\n' "$TRACE"
		case $2 in
		v:*) printf 'echo "%s"\n' "${2#v:}" ;;
		empty) : ;;
		endless) printf 'exec yes 0.15.0\n' ;;
		real) printf "printf '0.14.0-51-ge946197\\\\n'\\n" ;;
		nonl) printf 'printf %%s 0.15.0\n' ;;
		two) printf 'echo 0.15.0\necho 0.15.0\n' ;;
		exit3) printf 'echo 0.15.0\nexit 3\n' ;;
		big) printf 'yes 0.15.0 | head -c 6000\n' ;;
		hang) printf 'exec sleep 30\n' ;;
		esac
	} >"$1"
	chmod 0755 "$1"
}

# resign <key letter>: re-hash and re-sign the candidate fixture.
resign() {
	(cd "$FIX_DIR/agent" && sha256sum cadence-agent >cadence-agent.sha256)
	minisign -S -s "$T/keys/$1.key" -m "$FIX_DIR/agent/cadence-agent" \
		-x "$FIX_DIR/agent/cadence-agent.minisig" >/dev/null
}

# new_up_case <name> <candidate spec> [signing key letter]
new_up_case() {
	UPN=$((UPN + 1))
	new_case "up$UPN-$1" "${3:-a}"
	R=$CASE/root
	TRACE=$CASE/trace
	: >"$TRACE"
	mkdir -p "$R/etc/cadence" "$R/usr/bin" "$R/usr/local/bin"
	printf 'CADENCE_CLIENT_CERT_FILE=/x\n' >"$R/etc/cadence/agent.env"
	printf '%s\n' "$R" >"$FAKE_DIR/root"
	mk_agent "$FIX_DIR/agent/cadence-agent" "$2"
	resign "${3:-a}"
	make_up_copy "$R"
	INSTALL_UNDER_TEST=$CASE/install-under-test.sh
}

run_up() { run_install CADENCE_UPGRADE_ONLY=true CADENCE_PROBE_CANARY=leak "$@"; }

CANON() { printf '%s\n' "$R/usr/bin/cadence-agent"; }
LEGACY() { printf '%s\n' "$R/usr/local/bin/cadence-agent"; }
cand_ran() { grep -qF "$TD/" "$TRACE"; }
leaked() { grep -q 'canary=leak' "$TRACE"; }

# expect_up_pass <label> <message fragment>: the whole check passed and the
# install tripwire was reached; the candidate ran, and no probe saw the canary.
expect_up_pass() {
	if [ "$RC" -eq 99 ] && reached_install && no_unexpected && no_leftovers &&
		grep -qF -- "$2" "$OUT" && cand_ran && ! leaked; then
		ok "$1 (exit=$RC, install reached)"
	else
		fail "$1 (exit=$RC, want 99 with '$2'): $(cat "$OUT") trace: $(cat "$TRACE")"
	fi
}

# expect_up_refuse <label> <message fragment> <downloads>: exit 1, message,
# install never reached, nothing left behind, no probe saw the canary.
expect_up_refuse() {
	if [ "$RC" -eq 1 ] && ! reached_install && no_unexpected && no_leftovers &&
		grep -qF -- "$2" "$OUT" && [ "$(downloads)" -eq "$3" ] && ! leaked; then
		ok "$1 (exit=$RC, $3 download(s), install not reached)"
	else
		fail "$1 (exit=$RC, want 1 with '$2' and $3 download(s), got $(downloads)): $(cat "$OUT") trace: $(cat "$TRACE")"
	fi
}

PUB_A="CADENCE_MINISIGN_PUB=$T/keys/a.pub"

echo "6a. which version is the floor (candidate 0.15.0 unless stated)"
# floor_case <label> <canonical spec|-> <legacy spec|-> <candidate spec> pass|refuse <fragment>
floor_case() {
	new_up_case floor "$4"
	if [ "$2" != - ]; then mk_agent "$(CANON)" "$2"; fi
	if [ "$3" != - ]; then mk_agent "$(LEGACY)" "$3"; fi
	run_up "$PUB_A"
	if [ "$5" = pass ]; then
		expect_up_pass "$1" "$6"
	else
		expect_up_refuse "$1" "$6" 3
	fi
}
floor_case "historical path only, git describe build 0.14.0-51-ge946197 to 0.15.0 (the real fleet)" - v:0.14.0-51-ge946197 v:0.15.0 pass "downgrade check OK (host 0.14.0, server 0.15.0)"
# The exact output of a real host: env -i /usr/local/bin/cadence-agent -version
# printed "0.14.0-51-ge946197\n" on stdout with status 0 (checked on a fleet
# host). The fixture agent prints those bytes with printf, not echo.
new_up_case realbytes v:0.15.0
mk_agent "$(LEGACY)" real
if [ "$(env -i "$(LEGACY)" -version | wc -c | tr -d ' ')" = 19 ] &&
	[ "$(env -i "$(LEGACY)" -version)" = "0.14.0-51-ge946197" ]; then
	ok "fixture agent prints exactly 0.14.0-51-ge946197 and one newline (19 bytes)"
else
	fail "fixture agent output differs: $(env -i "$(LEGACY)" -version | od -An -c)"
fi
: >"$TRACE"
run_up "$PUB_A"
expect_up_pass "real host output, exact bytes 0.14.0-51-ge946197 to 0.15.0" "downgrade check OK (host 0.14.0, server 0.15.0)"
floor_case "canonical path only, older" v:0.14.0 - v:0.15.0 pass "downgrade check OK (host 0.14.0, server 0.15.0)"
floor_case "canonical path only, equal version" v:0.15.0 - v:0.15.0 pass "downgrade check OK (host 0.15.0, server 0.15.0)"
floor_case "canonical path only, describe build of the same version" v:0.15.0-3-gabcdef - v:0.15.0 pass "downgrade check OK (host 0.15.0, server 0.15.0)"
floor_case "canonical path only, newer host" v:0.15.0 - v:0.14.9 refuse "refusing to downgrade: the host runs 0.15.0, the server offers 0.14.9"
floor_case "historical path only, newer host" - v:0.15.0 v:0.14.9 refuse "refusing to downgrade: the host runs 0.15.0, the server offers 0.14.9"
floor_case "both paths, canonical newer: the maximum is the floor" v:0.15.2 v:0.14.0 v:0.15.1 refuse "the host runs 0.15.2, the server offers 0.15.1"
floor_case "both paths, historical newer: the maximum is the floor" v:0.14.0 v:0.15.2 v:0.15.1 refuse "the host runs 0.15.2, the server offers 0.15.1"
floor_case "both paths, candidate equal to the maximum" v:0.15.2 v:0.14.0 v:0.15.2 pass "downgrade check OK (host 0.15.2, server 0.15.2)"
floor_case "both paths, candidate above the maximum" v:0.14.0 v:0.15.2 v:0.15.3 pass "downgrade check OK (host 0.15.2, server 0.15.3)"
floor_case "minor and major are compared as integers, not text" v:0.9.0 v:0.10.0 v:0.9.9 refuse "the host runs 0.10.0, the server offers 0.9.9"
floor_case "major version above" v:1.0.0 - v:0.99.99 refuse "the host runs 1.0.0, the server offers 0.99.99"

# both agents are read, and executed, in every pass case above: check once
new_up_case both v:0.15.0
mk_agent "$(CANON)" v:0.14.0
mk_agent "$(LEGACY)" v:0.14.1
run_up "$PUB_A"
if grep -qF "$(CANON) " "$TRACE" && grep -qF "$(LEGACY) " "$TRACE"; then
	ok "both existing paths are read"
else
	fail "a path was ignored: $(cat "$TRACE")"
fi

echo "6b. a path that exists but cannot be trusted refuses, whatever the other one says"
# state_case <label> <message fragment> <setup command...>: canonical = valid
# 0.15.0, the historical path is put in the state set up by the command.
state_case() {
	_label=$1
	_frag=$2
	shift 2
	new_up_case state v:0.15.0
	mk_agent "$(CANON)" v:0.15.0
	: >"$TRACE"
	"$@"
	run_up "$PUB_A"
	expect_up_refuse "$_label" "$_frag" 0
}
set_symlink() { ln -s "$(CANON)" "$(LEGACY)"; }
set_dangling() { ln -s "$R/nonexistent" "$(LEGACY)"; }
set_directory() { mkdir "$(LEGACY)"; }
set_noexec() { mk_agent "$(LEGACY)" v:0.14.0; chmod 0644 "$(LEGACY)"; }
state_case "historical path is a symlink to a valid agent" "is not a regular executable file" set_symlink
state_case "historical path is a dangling symlink" "is not a regular executable file" set_dangling
state_case "historical path is a directory" "is not a regular executable file" set_directory
state_case "historical path is not executable" "is not a regular executable file" set_noexec
if grep -q "docs/agent-path-migration.md, section 6" "$OUT"; then
	ok "the refusal names the documented recovery"
else
	fail "no recovery pointer in: $(cat "$OUT")"
fi
if grep -qF "$R/usr/local/bin/cadence-agent" "$OUT"; then
	ok "the refusal names the offending path"
else
	fail "offending path not named in: $(cat "$OUT")"
fi

# the same, on the canonical path with a valid historical agent
new_up_case state2 v:0.15.0
mk_agent "$(LEGACY)" v:0.15.0
ln -s "$(LEGACY)" "$(CANON)"
run_up "$PUB_A"
expect_up_refuse "canonical path is a symlink to a valid agent" "is not a regular executable file" 0

# unreadable version on one path, the other one valid
bad_version_case() {
	new_up_case badver v:0.15.0
	mk_agent "$(CANON)" v:0.15.0
	mk_agent "$(LEGACY)" "$2"
	: >"$TRACE"
	_t0=$(date +%s)
	run_up "$PUB_A"
	_t1=$(date +%s)
	expect_up_refuse "historical path -version $1" "$3" 0
	# hang and endless would run 30 s, or fill the disk, without the timeout
	# and the output cap: the probe has to be cut within a few seconds
	case $2 in
	hang | endless)
		if [ $((_t1 - _t0)) -le 8 ]; then ok "historical path -version $1: cut short ($((_t1 - _t0)) s)"; else fail "historical path -version $1: took $((_t1 - _t0)) s"; fi
		;;
	esac
}
bad_version_case "prints nothing" empty "cannot read the version"
bad_version_case "has no trailing newline" nonl "cannot read the version"
bad_version_case "prints two lines" two "cannot read the version"
bad_version_case "exits non-zero" exit3 "cannot read the version"
bad_version_case "prints more than 4096 bytes" big "cannot read the version"
bad_version_case "does not return before the timeout" hang "cannot read the version"
bad_version_case "never stops writing" endless "cannot read the version"
bad_version_case "prints text" v:garbage "does not report a valid version"
bad_version_case "prints 0.14" v:0.14 "does not report a valid version"
bad_version_case "prints a leading zero (00.14.0)" v:00.14.0 "does not report a valid version"
bad_version_case "prints an empty describe suffix (0.14.0-)" v:0.14.0- "does not report a valid version"
bad_version_case "prints a version with a space" 'v:0.14.0 ' "does not report a valid version"

new_up_case none v:0.15.0
run_up "$PUB_A"
expect_up_refuse "no agent at either path" "found no agent binary" 0

new_up_case nopub v:0.15.0
mk_agent "$(LEGACY)" v:0.14.0
run_up
expect_up_refuse "no CADENCE_MINISIGN_PUB" "CADENCE_UPGRADE_ONLY requires CADENCE_MINISIGN_PUB" 0
if [ ! -s "$TRACE" ]; then ok "no CADENCE_MINISIGN_PUB: no binary was executed"; else fail "something ran: $(cat "$TRACE")"; fi

echo "6c. the downloaded binary"
# cand_case <label> <candidate spec> <message fragment>: floor valid at 0.14.0.
cand_case() {
	new_up_case cand "$2"
	mk_agent "$(LEGACY)" v:0.14.0
	_t0=$(date +%s)
	run_up "$PUB_A"
	_t1=$(date +%s)
	expect_up_refuse_run "candidate $1" "$3"
	case $2 in
	hang | endless)
		if [ $((_t1 - _t0)) -le 8 ]; then ok "candidate $1: cut short ($((_t1 - _t0)) s)"; else fail "candidate $1: took $((_t1 - _t0)) s"; fi
		;;
	esac
}
# like expect_up_refuse (3 downloads: binary, sha256, signature), and the
# candidate DID run, since it was verified first
expect_up_refuse_run() {
	if cand_ran; then
		expect_up_refuse "$1" "$2" 3
	else
		fail "$1: the verified candidate never ran: $(cat "$OUT")"
	fi
}
cand_case "prints nothing" empty "cannot report its version"
cand_case "has no trailing newline" nonl "cannot report its version"
cand_case "prints two lines" two "cannot report its version"
cand_case "exits non-zero" exit3 "cannot report its version"
cand_case "prints more than 4096 bytes" big "cannot report its version"
cand_case "does not return before the timeout" hang "cannot report its version"
cand_case "never stops writing" endless "cannot report its version"
cand_case "prints a describe suffix (0.15.0-3-gabc)" v:0.15.0-3-gabc "does not report a strict N.N.N version"
cand_case "prints 0.15" v:0.15 "does not report a strict N.N.N version"
cand_case "prints a leading zero (01.15.0)" v:01.15.0 "does not report a strict N.N.N version"
cand_case "prints text" v:garbage "does not report a strict N.N.N version"

# never executed before its signature and sha256 are verified
nevercase() {
	if cand_ran; then
		fail "$1: the candidate was executed: $(cat "$TRACE")"
	else
		ok "$1: the candidate was never executed"
	fi
}
new_up_case sigC v:0.15.0 c
mk_agent "$(LEGACY)" v:0.14.0
run_up "$PUB_A"
expect_up_refuse "candidate signed by a stranger" "minisign signature invalid" 3
nevercase "candidate signed by a stranger"

new_up_case sha v:0.15.0
mk_agent "$(LEGACY)" v:0.14.0
printf 'tampered\n' >"$FIX_DIR/agent/cadence-agent"
run_up "$PUB_A"
expect_up_refuse "altered candidate, sha256 mismatch" "checksum mismatch" 2
nevercase "altered candidate, sha256 mismatch"

new_up_case sig v:0.15.0
mk_agent "$(LEGACY)" v:0.14.0
printf 'tampered\n' >"$FIX_DIR/agent/cadence-agent"
(cd "$FIX_DIR/agent" && sha256sum cadence-agent >cadence-agent.sha256)
run_up "$PUB_A"
expect_up_refuse "altered candidate, sha256 still matching" "minisign signature invalid" 3
nevercase "altered candidate, sha256 still matching"

echo "6d. migration, then a second run on the migrated host"
BIN_CHAIN=$T/bin-chain
mkdir -p "$BIN_CHAIN"
cat >"$BIN_CHAIN/install" <<'EOF'
#!/bin/sh
# Real copy, but only ever into the case's fixture root.
mode=""
if [ "$1" = "-m" ]; then mode=$2; shift 2; fi
root=$(cat "$FAKE_DIR/root")
dst=$2
case $dst in
"$root"/*) ;;
*) dst=$root$dst ;;
esac
mkdir -p "$(dirname "$dst")"
cp "$1" "$dst"
if [ -n "$mode" ]; then chmod "$mode" "$dst"; fi
EOF
for t in systemctl update-ca-certificates apt-get; do printf '#!/bin/sh\nexit 0\n' >"$BIN_CHAIN/$t"; done
chmod +x "$BIN_CHAIN"/*

expect_chain() {
	if [ "$RC" -eq "$3" ] && no_unexpected && no_leftovers && grep -qF -- "$2" "$OUT"; then
		ok "$1 (exit=$RC)"
	else
		fail "$1 (exit=$RC, want $3 with '$2'): $(cat "$OUT")"
	fi
}

# chain_case <variant: kept|removed>
chain_case() {
	new_up_case chain-"$1" v:0.15.0
	mk_agent "$(LEGACY)" v:0.14.0-51-ge946197
	EXTRA_PATH=$BIN_CHAIN run_up "$PUB_A"
	unset EXTRA_PATH
	expect_chain "$1: first run migrates the historical host" "downgrade check OK (host 0.14.0, server 0.15.0)" 0
	if cmp -s "$FIX_DIR/agent/cadence-agent" "$(CANON)"; then
		ok "$1: the canonical path now holds the new binary"
	else
		fail "$1: canonical binary missing or different"
	fi
	if [ "$(grep -c 'canary=leak' "$TRACE")" -eq 1 ] && [ "$(tail -n 1 "$TRACE")" = "$(CANON) canary=leak" ]; then
		ok "$1: only the final, unprobed -version saw the canary (so the detector works and every probe was clean)"
	else
		fail "$1: unexpected canary pattern: $(cat "$TRACE")"
	fi
	if [ "$1" = removed ]; then rm -f "$(LEGACY)"; fi
	EXTRA_PATH=$BIN_CHAIN run_up "$PUB_A"
	unset EXTRA_PATH
	expect_chain "$1: second run at an equal version is accepted" "downgrade check OK (host 0.15.0, server 0.15.0)" 0
	cp "$(CANON)" "$CASE/canonical.after"
	mk_agent "$FIX_DIR/agent/cadence-agent" v:0.14.9
	resign a
	EXTRA_PATH=$BIN_CHAIN run_up "$PUB_A"
	unset EXTRA_PATH
	expect_chain "$1: a lower candidate is then refused" "refusing to downgrade: the host runs 0.15.0, the server offers 0.14.9" 1
	if cmp -s "$CASE/canonical.after" "$(CANON)"; then
		ok "$1: the refusal left the canonical binary untouched"
	else
		fail "$1: the canonical binary changed on a refusal"
	fi
}
chain_case kept
chain_case removed

echo "install-test: $pass passed, $failed failed"
[ "$failed" -eq 0 ]
