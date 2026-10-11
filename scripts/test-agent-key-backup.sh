#!/bin/sh
# test-agent-key-backup.sh -- tests for backup-signing-key.sh and
# restore-signing-key.sh (the offline backup key of the agent trust set).
#
# Needs minisign and git. No docker, no network, and never a real key: every
# key pair is generated here. The key password is fed to minisign by a wrapper
# placed first in PATH (minisign has no password option), so the scripts under
# test are the real ones and see a real password prompt. The password must
# never show up in any output. The signing tooling itself is covered by
# scripts/test-agent-signing.sh.
#
#   sh scripts/test-agent-key-backup.sh

set -eu

HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH='' cd -- "$HERE/.." && pwd)

command -v minisign >/dev/null 2>&1 || { echo "key-backup-test: minisign is required" >&2; exit 1; }
command -v git >/dev/null 2>&1 || { echo "key-backup-test: git is required" >&2; exit 1; }
REAL_MINISIGN=$(command -v minisign)
export REAL_MINISIGN

pass=0
failed=0

ok() { printf '  ok   %s\n' "$1"; pass=$((pass + 1)); }
fail() { printf '  FAIL %s\n' "$1"; failed=$((failed + 1)); }

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export HOME="$T/home"
mkdir -p "$HOME"

# shellcheck source=scripts/agent-sign-lib.sh
. "$HERE/agent-sign-lib.sh"

# --- keys: A active, B backup, C stranger ---------------------------------------
mkdir -p "$T/keys"
for k in a b c; do
	minisign -G -W -f -p "$T/keys/$k.pub" -s "$T/keys/$k.key" >/dev/null
done
# test oracle only: the ID minisign itself wrote in the comment line, completed
# with leading zeros to 16 characters (minisign 0.11 writes it without them for
# 1 key in 16, 0.12 pads; the scripts under test always print 16)
pad16() { _p=$1; while [ "${#_p}" -lt 16 ]; do _p=0$_p; done; printf '%s\n' "$_p"; }
idof() { pad16 "$(sed -n 1p "$T/keys/$1.pub" | awk '{print $NF}')"; }
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

# new_dir <name>: two fake binaries; sets D
new_dir() {
	D=$T/$1
	mkdir -p "$D"
	printf 'amd64 payload %s\n' "$1" >"$D/cadence-agent-linux-amd64"
	printf 'arm64 payload %s\n' "$1" >"$D/cadence-agent-linux-arm64"
}

# sb_pw <password> <secret key> <version> <dir>: sign_bundle against the test
# trust set with the password given to minisign through the wrapper (a fresh
# sh, so no environment leaks around). Sets RC, output in $OUT.
OUT=$T/out
sb_pw() {
	_p=$1
	shift
	RC=0
	with_pw "$_p" sh "$T/signcall.sh" "$HERE" "$T" "$@" >"$OUT" 2>&1 || RC=$?
}
# only_file <dir> <name>: the directory holds that one entry and nothing else
only_file() {
	_n=0
	for _f in "$1"/* "$1"/.[!.]*; do
		[ -e "$_f" ] || continue
		_n=$((_n + 1))
		[ "${_f##*/}" = "$2" ] || return 1
	done
	[ "$_n" -eq 1 ]
}

# --- 1. backup-signing-key.sh ---------------------------------------------------------------
echo "1. backup-signing-key.sh"
BACKUP=$HERE/backup-signing-key.sh
mkdir -p "$T/bk"
cp "$T/keys/b.key" "$T/bk/backup.key"
cp "$T/keys/a.key" "$T/bk/active.key"
cp "$T/keys/c.key" "$T/bk/stranger.key"

RC=0
with_pw "$PW" sh "$BACKUP" "$T/bk/backup.key" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -eq 0 ] && [ -f "$T/bk/backup.key.enc" ] && key_file_encrypted "$T/bk/backup.key.enc"; then
	ok "backup key: encrypted copy written (exit=$RC)"
else
	fail "backup of the backup key (exit=$RC): $(cat "$OUT")"
fi
if grep -q "is the trust set backup key $ID_B" "$OUT"; then ok "the log names the key: $ID_B (backup)"; else fail "no key id in the log: $(cat "$OUT")"; fi
if [ "$(stat -c %a "$T/bk/backup.key.enc")" = "600" ]; then ok "the copy is mode 600"; else fail "copy mode is $(stat -c %a "$T/bk/backup.key.enc")"; fi
if cmp -s "$T/bk/backup.key" "$T/keys/b.key"; then ok "the source key is untouched"; else fail "the source key changed"; fi
if grep -qF "$PW" "$OUT"; then fail "the password appears in the backup output"; else ok "the password does not appear in the backup output"; fi

RC=0
with_pw "$PW" sh "$BACKUP" "$T/bk/stranger.key" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -ne 0 ] && [ ! -e "$T/bk/stranger.key.enc" ] && grep -q "not one of the keys the agent trusts" "$OUT"; then
	ok "a key outside the trust set is refused, nothing written (exit=$RC)"
else
	fail "stranger key backup (exit=$RC): $(cat "$OUT")"
fi

RC=0
with_pw "$PW" sh "$BACKUP" "$T/bk/active.key" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -eq 0 ] && grep -q "trust set active key $ID_A" "$OUT"; then ok "the active key can be backed up too, named as active (exit=$RC)"; else fail "active key backup (exit=$RC): $(cat "$OUT")"; fi

cp "$T/bk/backup.key.enc" "$T/bk/before.enc"
RC=0
with_pw "$PW" sh "$BACKUP" "$T/bk/backup.key" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -ne 0 ] && cmp -s "$T/bk/before.enc" "$T/bk/backup.key.enc"; then ok "an existing .enc is not overwritten without --force (exit=$RC)"; else fail "overwrite without --force (exit=$RC)"; fi
RC=0
with_pw "$PW" sh "$BACKUP" --force "$T/bk/backup.key" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -eq 0 ] && key_file_encrypted "$T/bk/backup.key.enc"; then ok "--force replaces it (exit=$RC)"; else fail "--force (exit=$RC): $(cat "$OUT")"; fi

RC=0
sh "$BACKUP" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -eq 2 ]; then ok "no key argument: usage, no default location (exit=$RC)"; else fail "no argument (exit=$RC)"; fi
RC=0
sh "$BACKUP" "$T/bk/absent.key" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -eq 1 ]; then ok "absent key file refused (exit=$RC)"; else fail "absent key file (exit=$RC)"; fi

# --- 2. restore-signing-key.sh ----------------------------------------------------------------
echo "2. restore-signing-key.sh"
RESTORE=$HERE/restore-signing-key.sh
SRC=$T/bk/backup.key.enc
mkdir -p "$T/shm"

RC=0
with_pw "$PW" sh "$RESTORE" --to "$T/shm/restored.key" "$SRC" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -eq 0 ] && [ -f "$T/shm/restored.key" ]; then ok "restore to an explicit destination (exit=$RC)"; else fail "restore (exit=$RC): $(cat "$OUT")"; fi
if grep -q "trust set backup key $ID_B" "$OUT"; then ok "the log names the key: $ID_B (backup)"; else fail "no key id: $(cat "$OUT")"; fi
if [ "$(stat -c %a "$T/shm/restored.key")" = "600" ]; then ok "the restored key is mode 600"; else fail "mode $(stat -c %a "$T/shm/restored.key")"; fi
if key_file_encrypted "$T/shm/restored.key" && cmp -s "$SRC" "$T/shm/restored.key"; then ok "it stays password protected (same bytes as the backup)"; else fail "restored key is not the encrypted backup"; fi
if only_file "$T/shm" restored.key; then ok "no temporary file left in the destination directory"; else fail "leftovers: $(ls -A "$T/shm")"; fi
if grep -q "/dev/shm" "$OUT" && grep -q "delete it after use" "$OUT"; then ok "the final message recommends /dev/shm and deletion"; else fail "no tmpfs advice"; fi
if grep -qF "$PW" "$OUT"; then fail "the password appears in the restore output"; else ok "the password does not appear in the restore output"; fi

# the restored key really signs a release (the break-glass flow, end to end)
new_dir rs1
sb_pw "$PW" "$T/shm/restored.key" 0.15.0 "$D"
if [ "$RC" -eq 0 ] && grep -q "verified by key $ID_B (backup)" "$OUT"; then ok "the restored key signs a bundle that verifies as the backup key (exit=$RC)"; else fail "restored key signing (exit=$RC): $(cat "$OUT")"; fi

RC=0
with_pw "$PW" sh "$RESTORE" --to "$T/shm/restored.key" "$SRC" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -ne 0 ] && grep -q "already exists" "$OUT"; then ok "an existing destination is refused without --force (exit=$RC)"; else fail "existing destination (exit=$RC)"; fi
RC=0
with_pw "$PW" sh "$RESTORE" --force --to "$T/shm/restored.key" "$SRC" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -eq 0 ]; then ok "--force replaces it (exit=$RC)"; else fail "--force (exit=$RC): $(cat "$OUT")"; fi

RC=0
with_pw "wrong password" sh "$RESTORE" --to "$T/shm/wrongpw.key" "$SRC" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -ne 0 ] && [ ! -e "$T/shm/wrongpw.key" ] && grep -q "could not unlock" "$OUT"; then
	ok "wrong password: refused, nothing written (exit=$RC)"
else
	fail "wrong password (exit=$RC): $(cat "$OUT")"
fi
if only_file "$T/shm" restored.key; then ok "wrong password leaves no temporary file"; else fail "leftovers after a wrong password: $(ls -A "$T/shm")"; fi

# a key that is not in the trust set, protected like a real backup
cp "$T/keys/c.key" "$T/bk/stranger-enc.key"
with_pw "$PW" minisign -C -s "$T/bk/stranger-enc.key" >/dev/null 2>&1
RC=0
with_pw "$PW" sh "$RESTORE" --to "$T/shm/stranger.key" "$T/bk/stranger-enc.key" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -ne 0 ] && [ ! -e "$T/shm/stranger.key" ] && grep -q "not one of the keys the agent trusts" "$OUT"; then
	ok "a key outside the trust set is refused, nothing written (exit=$RC)"
else
	fail "stranger key restore (exit=$RC): $(cat "$OUT")"
fi

RC=0
sh "$RESTORE" "$SRC" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -ne 0 ] && grep -q -e "--to" "$OUT"; then ok "no --to: refused, there is no default destination (exit=$RC)"; else fail "no --to (exit=$RC)"; fi

RC=0
sh "$RESTORE" --passwordless --to "$T/shm/x.key" "$SRC" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -eq 2 ] && [ ! -e "$T/shm/x.key" ]; then ok "there is no --passwordless option (exit=$RC)"; else fail "--passwordless (exit=$RC)"; fi

RC=0
with_pw "$PW" sh "$RESTORE" --to "$T/shm/plain.key" "$T/keys/b.key" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -ne 0 ] && [ ! -e "$T/shm/plain.key" ] && grep -q "not password protected" "$OUT"; then
	ok "an unprotected source key is refused (exit=$RC)"
else
	fail "plain source (exit=$RC): $(cat "$OUT")"
fi

RC=0
with_pw "$PW" sh "$RESTORE" --to "$T/absent-dir/k.key" "$SRC" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -ne 0 ] && grep -q "does not exist" "$OUT"; then ok "a destination directory that does not exist is refused (exit=$RC)"; else fail "missing destination directory (exit=$RC)"; fi

ln -s "$T/shm/target" "$T/shm/link.key"
RC=0
with_pw "$PW" sh "$RESTORE" --force --to "$T/shm/link.key" "$SRC" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -ne 0 ] && [ ! -e "$T/shm/target" ]; then ok "a symbolic link as destination is refused (exit=$RC)"; else fail "symlink destination (exit=$RC)"; fi

# destinations inside git repositories: all refused
gitq() { git -c user.email=t@t -c user.name=t "$@"; }
mkdir -p "$T/gitrepo/sub"
(cd "$T/gitrepo" && gitq init -q && gitq commit -q --allow-empty -m init) >/dev/null 2>&1
gitq -C "$T/gitrepo" worktree add -q "$T/wt" >/dev/null 2>&1 || gitq -C "$T/gitrepo" worktree add -q -b wtbranch "$T/wt" >/dev/null 2>&1
gitq init -q --bare "$T/bare.git" >/dev/null 2>&1
for d in "$T/gitrepo" "$T/gitrepo/sub" "$T/wt" "$T/bare.git"; do
	RC=0
	with_pw "$PW" sh "$RESTORE" --to "$d/k.key" "$SRC" >"$OUT" 2>&1 || RC=$?
	if [ "$RC" -ne 0 ] && [ ! -e "$d/k.key" ] && grep -q "inside a git repository" "$OUT"; then
		ok "destination inside a git repository refused: ${d#"$T"/} (exit=$RC)"
	else
		fail "git destination ${d#"$T"/} (exit=$RC): $(cat "$OUT")"
	fi
done
# and the committed checkout itself, without ever writing there
RC=0
with_pw "$PW" sh "$RESTORE" --to "$REPO/k.key" "$SRC" >"$OUT" 2>&1 || RC=$?
if [ "$RC" -ne 0 ] && [ ! -e "$REPO/k.key" ] && grep -q "inside a git repository" "$OUT"; then
	ok "destination in this repository's checkout refused (exit=$RC)"
else
	fail "destination in the checkout (exit=$RC)"
	[ -e "$REPO/k.key" ] && rm -f -- "$REPO/k.key"
fi

echo "key-backup-test: $pass passed, $failed failed"
[ "$failed" -eq 0 ]
