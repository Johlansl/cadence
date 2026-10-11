#!/bin/sh
# test-script-hygiene.sh -- the classes of defect found by the audit of the
# shell scripts, so they cannot come back unnoticed.
#
#   1. a signal trap that only cleans up: the script carries on afterwards
#      (checked statically on every script, and the check itself is tested)
#   2. rotate-dashboard-password.sh leaving a copy of .env in /tmp when it fails
#   3. backup.sh pruning a directory it did not create
#
# The scripts run in throwaway trees with fake tools; nothing real is touched.
#
#   sh scripts/test-script-hygiene.sh

set -eu

HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH='' cd -- "$HERE/.." && pwd)

pass=0
failed=0

ok() { printf '  ok   %s\n' "$1"; pass=$((pass + 1)); }
fail() { printf '  FAIL %s\n' "$1"; failed=$((failed + 1)); }

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "1. signal traps"
# trap_violations <file>...: the lines that trap HUP, INT or TERM (alone or
# next to EXIT) with a handler that does not exit. After such a trap the
# interrupted script goes on running (in dash and bash alike), usually with its
# working directory already deleted. `trap - SIG` (a reset) is fine.
trap_violations() {
	grep -n -E "^[[:space:]]*trap[[:space:]]+('[^']*'|\"[^\"]*\"|[A-Za-z_][A-Za-z0-9_]*)[[:space:]]+([A-Z0-9]+[[:space:]]+)*(HUP|INT|TERM|QUIT)([[:space:]]|$)" "$@" /dev/null |
		grep -v -E "trap[[:space:]]+-[[:space:]]" |
		grep -v -E "exit [0-9]+" || true
}

# the check must see what it is meant to see
printf '%s\n' \
	"trap 'rm -rf \"\$T\"' EXIT INT TERM" \
	"trap cleanup INT TERM" \
	"	trap 'kill \$P; rm -rf \"\$T\"' EXIT HUP INT TERM" >"$T/bad.sh"
printf '%s\n' \
	"trap 'rm -rf \"\$T\"' EXIT" \
	"trap 'exit 130' INT" \
	"trap 'cleanup; exit 143' TERM" \
	"trap - EXIT INT TERM" \
	"trap cleanup EXIT" >"$T/good.sh"
if [ "$(trap_violations "$T/bad.sh" | wc -l | tr -d ' ')" -eq 3 ]; then
	ok "the check flags the three doomed forms"
else
	fail "the check missed a form: $(trap_violations "$T/bad.sh")"
fi
if [ -z "$(trap_violations "$T/good.sh")" ]; then
	ok "the check accepts the exit-on-signal forms and the reset"
else
	fail "the check flags a good form: $(trap_violations "$T/good.sh")"
fi

# every script of the repository: scripts, the caddy entrypoint, packaging
cd "$REPO"
all=$(ls scripts/*.sh caddy/*.sh packaging/*.sh 2>/dev/null)
# shellcheck disable=SC2086 # repo paths, no spaces: split on purpose
found=$(trap_violations $all)
if [ -z "$found" ]; then
	ok "no script traps a signal without exiting"
else
	fail "signal traps that do not exit: $found"
fi

echo "2. rotate-dashboard-password.sh leaves no copy of .env behind"
R=$T/rot
mkdir -p "$R/scripts" "$R/fakebin" "$R/tmp"
cp "$HERE/rotate-dashboard-password.sh" "$R/scripts/"
printf '%s\n' 'CADENCE_DASHBOARD_USER=cadence' 'CADENCE_DASHBOARD_PASSWORD_HASH=oldhash' 'SECRET_TOKEN=do-not-leak-me' >"$R/.env"
cp "$R/.env" "$R/.env.original"
cat >"$R/fakebin/docker" <<'EOF'
#!/bin/sh
printf '%s\n' '$2a$14$abcdefghijklmnopqrstuuABCDEFGHIJKLMNOPQRSTUVWXYZ01234'
EOF
REAL_SED=$(command -v sed)
cat >"$R/fakebin/sed" <<EOF
#!/bin/sh
# fails only for the call that rewrites .env, delegates every other call
case \$1 in
s\|^CADENCE_DASHBOARD_PASSWORD_HASH=*) exit 1 ;;
esac
exec $REAL_SED "\$@"
EOF
chmod +x "$R/fakebin/docker" "$R/fakebin/sed"

RC=0
PATH="$R/fakebin:$PATH" TMPDIR="$R/tmp" sh "$R/scripts/rotate-dashboard-password.sh" >"$R/out" 2>&1 || RC=$?
if [ "$RC" -ne 0 ]; then ok "a failing sed makes the script fail (exit=$RC)"; else fail "script succeeded with a failing sed: $(cat "$R/out")"; fi
if [ -z "$(ls -A "$R/tmp")" ]; then
	ok "the temporary copy of .env was removed"
else
	fail "left in the temp dir: $(ls -A "$R/tmp")"
fi
if cmp -s "$R/.env" "$R/.env.original"; then ok ".env is unchanged after the failure"; else fail ".env changed after a failure"; fi

# the normal run (real sed again) still works and leaves nothing either
rm -f "$R/fakebin/sed"
rm -f "$R"/.env.bak.*
cp "$R/.env.original" "$R/.env"
RC=0
PATH="$R/fakebin:$PATH" TMPDIR="$R/tmp" sh "$R/scripts/rotate-dashboard-password.sh" >"$R/out2" 2>&1 || RC=$?
# the single quotes are on purpose: the pattern holds literal dollar signs
# shellcheck disable=SC2016
if [ "$RC" -eq 0 ] && grep -q '^CADENCE_DASHBOARD_PASSWORD_HASH=\$\$2a\$\$14\$\$' "$R/.env" && [ -z "$(ls -A "$R/tmp")" ]; then
	ok "a normal run rewrites the hash (doubled dollars) and leaves nothing in the temp dir"
else
	fail "normal run (exit=$RC): $(cat "$R/out2") temp: $(ls -A "$R/tmp")"
fi

echo "3. backup.sh prunes only the timestamped directories it made"
B=$T/bk
mkdir -p "$B/target"
echo precious >"$B/target/file"
for d in 20240101T000000Z 20240102T000000Z 20240103T000000Z 20240104T000000Z \
	20240105T000000Z 20240106T000000Z 20240107T000000Z 20240108T000000Z \
	20240109T000000Z 20240110T000000Z 20240111T000000Z 20240112T000000Z \
	20240113T000000Z 20240114T000000Z 20240115T000000Z 20240116T000000Z; do
	mkdir -p "$B/$d"
done
# things that are not backups, some of them named almost like one
for d in precious home 20240101 T000000Z 20240101T000000 20240101T000000Zold; do
	mkdir -p "$B/$d"
	echo keep >"$B/$d/file"
done
# an old symlink with a timestamp name: must be skipped, never followed
ln -s "$B/target" "$B/20230101T000000Z"

# the real pruning block of backup.sh, extracted (never retyped)
block=$(sed -n '/^# 5\. Prune old backups/,/^fi$/p' "$HERE/backup.sh")
if [ -z "$block" ]; then
	fail "could not find the pruning block in backup.sh"
else
	# both are read by the extracted block
	# shellcheck disable=SC2034
	backup_dir=$B
	# shellcheck disable=SC2034
	keep=14
	eval "$block" >"$T/prune.log" 2>&1 || fail "the pruning block failed: $(cat "$T/prune.log")"
	left=0
	for d in "$B"/[0-9]*T*Z; do
		if [ -d "$d" ] && [ ! -L "$d" ]; then left=$((left + 1)); fi
	done
	if [ "$left" -eq 14 ] && [ ! -e "$B/20240101T000000Z" ] && [ ! -e "$B/20240102T000000Z" ] && [ -d "$B/20240103T000000Z" ]; then
		ok "the two oldest timestamped directories were pruned, fourteen remain"
	else
		fail "unexpected pruning result: $left left: $(ls "$B")"
	fi
	foreign_ok=1
	for d in precious home 20240101 T000000Z 20240101T000000 20240101T000000Zold; do
		[ -f "$B/$d/file" ] || foreign_ok=0
	done
	if [ "$foreign_ok" -eq 1 ]; then ok "directories that are not backups were left alone"; else fail "a directory that is not a backup was pruned: $(ls "$B")"; fi
	if [ -L "$B/20230101T000000Z" ] && [ -f "$B/target/file" ]; then
		ok "a symlink named like a backup was neither followed nor removed"
	else
		fail "the symlink or its target is gone"
	fi
fi

echo "hygiene-test: $pass passed, $failed failed"
[ "$failed" -eq 0 ]
