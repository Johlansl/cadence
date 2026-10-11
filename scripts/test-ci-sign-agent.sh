#!/bin/sh
# test-ci-sign-agent.sh -- tests for ci-sign-agent.sh, with throwaway keys.
#
# Needs minisign. No network, no real key: every key pair is generated here,
# unencrypted (minisign -G -W), like the CI signing key.
#
#   sh scripts/test-ci-sign-agent.sh

set -eu

HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
SIGN=$HERE/ci-sign-agent.sh

pass=0
failed=0

ok() { printf '  ok   %s\n' "$1"; pass=$((pass + 1)); }
fail() { printf '  FAIL %s\n' "$1"; failed=$((failed + 1)); }

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Two independent key pairs: "good" signs, "other" is a stranger.
mkdir -p "$T/keys"
minisign -G -W -f -p "$T/keys/good.pub" -s "$T/keys/good.key" >/dev/null
minisign -G -W -f -p "$T/keys/other.pub" -s "$T/keys/other.key" >/dev/null

# new_case <name> -- fresh dist dir with two fake binaries, hashes in
# H_AMD64 / H_ARM64.
new_case() {
	D=$T/$1
	mkdir -p "$D"
	printf 'amd64 payload %s\n' "$1" >"$D/cadence-agent-linux-amd64"
	printf 'arm64 payload %s\n' "$1" >"$D/cadence-agent-linux-arm64"
	H_AMD64=$(sha256sum "$D/cadence-agent-linux-amd64" | cut -d' ' -f1)
	H_ARM64=$(sha256sum "$D/cadence-agent-linux-arm64" | cut -d' ' -f1)
	OUT=$T/$1.out
}

# run_sign <pub> <key> <dir> <arch=hash>... -- sets RC, output in $OUT.
run_sign() {
	pub=$1
	key=$2
	dir=$3
	shift 3
	RC=0
	sh "$SIGN" 0.15.0 "$pub" "$key" "$dir" "$@" >"$OUT" 2>&1 || RC=$?
}

no_sigs() {
	[ ! -e "$1/cadence-agent-linux-amd64.minisig" ] &&
		[ ! -e "$1/cadence-agent-linux-arm64.minisig" ]
}

# --- nominal -------------------------------------------------------------
new_case nominal
run_sign "$T/keys/good.pub" "$T/keys/good.key" "$D" "amd64=$H_AMD64" "arm64=$H_ARM64"
if [ "$RC" -eq 0 ]; then ok "nominal: exit 0"; else fail "nominal: exit $RC: $(cat "$OUT")"; fi

for a in amd64 arm64; do
	if minisign -V -p "$T/keys/good.pub" -x "$D/cadence-agent-linux-$a.minisig" \
		-m "$D/cadence-agent-linux-$a" -q; then
		ok "nominal: $a signature verifies independently"
	else
		fail "nominal: $a signature does not verify"
	fi
	h=$H_AMD64
	[ "$a" = arm64 ] && h=$H_ARM64
	want="trusted comment: cadence-agent 0.15.0 linux/$a sha256:$h"
	got=$(sed -n 3p "$D/cadence-agent-linux-$a.minisig")
	if [ "$got" = "$want" ]; then
		ok "nominal: $a trusted comment has the exact format"
	else
		fail "nominal: $a trusted comment is '$got', want '$want'"
	fi
done

# the other key must NOT verify what the good key signed
if minisign -V -p "$T/keys/other.pub" -x "$D/cadence-agent-linux-amd64.minisig" \
	-m "$D/cadence-agent-linux-amd64" -q 2>/dev/null; then
	fail "nominal: a stranger key verifies the signature"
else
	ok "nominal: a stranger key does not verify the signature"
fi

# the secret key material must never reach the output
keyline=$(sed -n 2p "$T/keys/good.key")
if grep -qF -- "$keyline" "$OUT"; then
	fail "nominal: secret key material in the output"
else
	ok "nominal: secret key material not in the output"
fi

# single arch is accepted (the script is not hard-wired to two)
new_case single
run_sign "$T/keys/good.pub" "$T/keys/good.key" "$D" "arm64=$H_ARM64"
if [ "$RC" -eq 0 ] && [ -f "$D/cadence-agent-linux-arm64.minisig" ] &&
	[ ! -e "$D/cadence-agent-linux-amd64.minisig" ]; then
	ok "single arch: only that arch is signed"
else
	fail "single arch: rc=$RC: $(cat "$OUT")"
fi

# --- expected hash differs: refuse, sign nothing ---------------------------
new_case badhash
other=$(printf 'tampered' | sha256sum | cut -d' ' -f1)
run_sign "$T/keys/good.pub" "$T/keys/good.key" "$D" "amd64=$H_AMD64" "arm64=$other"
if [ "$RC" -ne 0 ]; then ok "hash mismatch: non-zero exit"; else fail "hash mismatch: exit 0"; fi
if no_sigs "$D"; then
	ok "hash mismatch: nothing signed (not even the arch whose hash matched)"
else
	fail "hash mismatch: a signature was written"
fi
if grep -q "sha256 mismatch" "$OUT"; then
	ok "hash mismatch: error names the cause"
else
	fail "hash mismatch: unexpected message: $(cat "$OUT")"
fi

# binary modified after the build job reported its hash
new_case modified
printf 'x' >>"$D/cadence-agent-linux-amd64"
run_sign "$T/keys/good.pub" "$T/keys/good.key" "$D" "amd64=$H_AMD64" "arm64=$H_ARM64"
if [ "$RC" -ne 0 ] && no_sigs "$D"; then
	ok "modified binary: refused, nothing signed"
else
	fail "modified binary: rc=$RC"
fi

# --- secret key does not match the public key -------------------------------
new_case mismatch
run_sign "$T/keys/good.pub" "$T/keys/other.key" "$D" "amd64=$H_AMD64" "arm64=$H_ARM64"
if [ "$RC" -ne 0 ]; then ok "key mismatch: non-zero exit"; else fail "key mismatch: exit 0"; fi
if no_sigs "$D"; then
	ok "key mismatch: unverified signatures removed"
else
	fail "key mismatch: signatures left behind"
fi
if grep -q "does not verify" "$OUT"; then
	ok "key mismatch: failure is at self-verification"
else
	fail "key mismatch: unexpected message: $(cat "$OUT")"
fi

# --- secret key file absent or empty ----------------------------------------
new_case nokey
run_sign "$T/keys/good.pub" "$T/keys/absent.key" "$D" "amd64=$H_AMD64" "arm64=$H_ARM64"
if [ "$RC" -ne 0 ] && no_sigs "$D"; then
	ok "absent secret key: refused, nothing signed"
else
	fail "absent secret key: rc=$RC"
fi

new_case emptykey
: >"$T/keys/empty.key"
run_sign "$T/keys/good.pub" "$T/keys/empty.key" "$D" "amd64=$H_AMD64" "arm64=$H_ARM64"
if [ "$RC" -ne 0 ] && no_sigs "$D"; then
	ok "empty secret key: refused, nothing signed"
else
	fail "empty secret key: rc=$RC"
fi

# --- argument validation ------------------------------------------------------
new_case args
run_sign "$T/keys/absent.pub" "$T/keys/good.key" "$D" "amd64=$H_AMD64"
if [ "$RC" -ne 0 ] && no_sigs "$D"; then ok "absent public key: refused"; else fail "absent public key: rc=$RC"; fi

run_sign "$T/keys/good.pub" "$T/keys/good.key" "$D" "riscv64=$H_AMD64"
if [ "$RC" -ne 0 ]; then ok "unsupported arch: refused"; else fail "unsupported arch: exit 0"; fi

run_sign "$T/keys/good.pub" "$T/keys/good.key" "$D" "amd64=$H_AMD64" "amd64=$H_AMD64"
if [ "$RC" -ne 0 ] && no_sigs "$D"; then ok "duplicate arch: refused"; else fail "duplicate arch: rc=$RC"; fi

upper=$(printf '%s' "$H_AMD64" | tr 'a-f' 'A-F')
run_sign "$T/keys/good.pub" "$T/keys/good.key" "$D" "amd64=$upper"
if [ "$RC" -ne 0 ] && no_sigs "$D"; then ok "uppercase hash: refused"; else fail "uppercase hash: rc=$RC"; fi

run_sign "$T/keys/good.pub" "$T/keys/good.key" "$D" "amd64=abc123"
if [ "$RC" -ne 0 ] && no_sigs "$D"; then ok "short hash: refused"; else fail "short hash: rc=$RC"; fi

run_sign "$T/keys/good.pub" "$T/keys/good.key" "$D" "amd64"
if [ "$RC" -ne 0 ]; then ok "argument without hash: refused"; else fail "argument without hash: exit 0"; fi

# version grammar: strict canonical N.N.N, same as publish-agent.sh
for v in 0.15.0-rc 1.0 01.2.3 1.2.3.4 1.02.3 1.2.03 v1.2.3 1.2.3+b .1.2 1..2 ""; do
	new_case badver
	RC=0
	sh "$SIGN" "$v" "$T/keys/good.pub" "$T/keys/good.key" "$D" "amd64=$H_AMD64" >"$OUT" 2>&1 || RC=$?
	if [ "$RC" -ne 0 ] && no_sigs "$D" && grep -q "not strict canonical" "$OUT"; then
		ok "version '$v': refused"
	else
		fail "version '$v': rc=$RC"
	fi
done
for v in 0.15.0 0.0.0 10.20.30 1.0.0; do
	new_case goodver
	RC=0
	sh "$SIGN" "$v" "$T/keys/good.pub" "$T/keys/good.key" "$D" "amd64=$H_AMD64" >"$OUT" 2>&1 || RC=$?
	if [ "$RC" -eq 0 ] && [ -f "$D/cadence-agent-linux-amd64.minisig" ]; then
		ok "version '$v': accepted"
	else
		fail "version '$v': rc=$RC: $(cat "$OUT")"
	fi
done

# --- shared definitions: one lib, plus copies that must stay in step ---------
# check_version lives in agent-version-lib.sh and is SOURCED by publish-agent.sh,
# ci-sign-agent.sh and check-release-source.sh: prove each of them uses it and
# keeps no copy, then feed the same list of versions to the lib and to the copy
# that agent-install.sh must keep (it runs alone on hosts, without a checkout).
# The same is done for the key ID, which has three copies for the same reason.
LIBV=$HERE/agent-version-lib.sh
for s in publish-agent.sh ci-sign-agent.sh check-release-source.sh; do
	if grep -q 'agent-version-lib.sh"$' "$HERE/$s" && ! grep -q '^check_version() {' "$HERE/$s"; then
		ok "$s sources agent-version-lib.sh and keeps no copy of check_version"
	else
		fail "$s defines its own check_version or does not source the lib"
	fi
done

eval "$(sed -n '/^check_version() {/,/^}/p' "$LIBV")"
eval "$(sed -n '/^version_triple() {/,/^}/p' "$HERE/agent-install.sh")"
eval "$(sed -n '/^version_current() {/,/^}/p' "$HERE/agent-install.sh")"
drift=0
# <version> followed by 1 when the grammar accepts it, 0 when it refuses it
while IFS='|' read -r v want; do
	a=0
	b=0
	check_version "$v" && a=1
	version_triple "$v" && b=1
	if [ "$a" != "$want" ] || [ "$b" != "$want" ]; then
		drift=1
		echo "  grammar differs on '$v': want $want, lib=$a agent-install=$b"
	fi
done <<'EOF'
0.15.0|1
0.0.0|1
10.20.30|1
1.0.0|1
123456789.0.1|1
|0
1|0
1.0|0
01.2.3|0
00.0.0|0
0.00.0|0
0.15.0-rc|0
1.2.3.4|0
1.02.3|0
1.2.03|0
.1.2|0
1.2.|0
1..2|0
a.b.c|0
1.2.3-x|0
1.2.3 |0
 1.2.3|0
+1.2.3|0
1.2.+3|0
v1.2.3|0
EOF
if [ "$drift" -eq 0 ]; then ok "version grammar: the lib and the agent-install.sh copy agree on every sample"; else fail "version grammar drifted between the lib and agent-install.sh"; fi
# the one difference on purpose: shell integers stay exact, so the installer
# stops at 9 digits per component while the lib has no such limit
if check_version 1234567890.0.0 && ! version_triple 1234567890.0.0; then
	ok "version grammar: the installer refuses a 10 digit component, as documented"
else
	fail "version grammar: the documented 9 digit limit of the installer changed"
fi
# the installer also reads the version of a running agent (git describe builds)
drift=0
while IFS='|' read -r v want; do
	a=0
	version_current "$v" && a=1
	[ "$a" = "$want" ] || { drift=1; echo "  version_current on '$v': want $want got $a"; }
done <<'EOF'
0.14.0|1
0.14.0-51-ge946197|1
0.15.0-3-gabc|1
0.14.0-|0
-5|0
0.14|0
01.2.3-x|0
1.2.3-|0
|0
EOF
if [ "$drift" -eq 0 ]; then ok "version_current: the installer reads a running agent's version as the agent does"; else fail "version_current drifted"; fi

# key ID: agent-install.sh and agent-sign-lib.sh work on a base64 line,
# publish-agent.sh on a .pub file. Same keys, same expected IDs, taken from
# the comment minisign itself wrote (the oracle: the scripts never trust it),
# completed with leading zeros to 16 characters: minisign 0.11 writes the ID
# without them for 1 key in 16, 0.12 pads, and the three copies print 16.
pad16() { _p=$1; while [ "${#_p}" -lt 16 ]; do _p=0$_p; done; printf '%s\n' "$_p"; }
eval "$(sed -n '/^key_id() {/,/^}/p' "$HERE/agent-install.sh" | sed 's/^key_id()/inst_key_id()/')"
eval "$(sed -n '/^key_id() {/,/^}/p' "$HERE/agent-sign-lib.sh" | sed 's/^key_id()/lib_key_id()/')"
eval "$(sed -n '/^pubkey_id() {/,/^}/p' "$HERE/publish-agent.sh")"
drift=0
n=0
for pub in "$T/keys/good.pub" "$HERE/../agent/minisign.pub" "$HERE/../agent/minisign-backup.pub"; do
	line=$(sed -n 2p "$pub")
	oracle=$(pad16 "$(sed -n 1p "$pub" | sed 's/^.*key //')")
	i=$(inst_key_id "$line")
	l=$(lib_key_id "$line")
	p=$(pubkey_id "$pub" 2>/dev/null) || p="refused"
	n=$((n + 1))
	if [ "$i" != "$oracle" ] || [ "$l" != "$oracle" ] || [ "$p" != "$oracle" ]; then
		drift=1
		echo "  key ID of $pub: want $oracle, agent-install=$i sign-lib=$l publish-agent=$p"
	fi
done
if [ "$drift" -eq 0 ] && [ "$n" -eq 3 ]; then ok "key ID: the three copies agree with minisign on $n keys"; else fail "key ID copies drifted"; fi
# and on a signature line: the ID the signature claims is the signing key's
echo payload >"$T/idpayload"
minisign -S -s "$T/keys/good.key" -m "$T/idpayload" -x "$T/idpayload.minisig" >/dev/null 2>&1
sigline=$(sed -n 2p "$T/idpayload.minisig")
oracle=$(pad16 "$(sed -n 1p "$T/keys/good.pub" | sed 's/^.*key //')")
if [ "$(inst_key_id "$sigline")" = "$oracle" ] && [ "$(lib_key_id "$sigline")" = "$oracle" ]; then
	ok "key ID: the two base64 copies read the ID of a signature line"
else
	fail "key ID of a signature line: install=$(inst_key_id "$sigline") lib=$(lib_key_id "$sigline") want $oracle"
fi
# a line that is not a key: every copy says "?" or refuses, none invents an ID
if [ "$(inst_key_id "not base64 at all")" = "?" ] && [ "$(lib_key_id "not base64 at all")" = "?" ]; then
	ok "key ID: both base64 copies answer ? for garbage"
else
	fail "key ID: a copy invented an ID for garbage"
fi

new_case spacever
RC=0
sh "$SIGN" "0.15.0 x" "$T/keys/good.pub" "$T/keys/good.key" "$D" "amd64=$H_AMD64" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -ne 0 ] && no_sigs "$D"; then ok "version with a space: refused"; else fail "version with a space: rc=$RC"; fi

RC=0
sh "$SIGN" 0.15.0 >"$OUT" 2>&1 || RC=$?
if [ "$RC" -eq 2 ]; then ok "too few arguments: usage, exit 2"; else fail "too few arguments: rc=$RC"; fi

# a path with a space must work (unquoted-variable regression guard)
new_case "spaced dir"
run_sign "$T/keys/good.pub" "$T/keys/good.key" "$D" "amd64=$H_AMD64" "arm64=$H_ARM64"
if [ "$RC" -eq 0 ] && [ -f "$D/cadence-agent-linux-amd64.minisig" ]; then
	ok "directory with a space in its name"
else
	fail "directory with a space: rc=$RC: $(cat "$OUT")"
fi

echo "ci-sign-test: $pass passed, $failed failed"
[ "$failed" -eq 0 ]
