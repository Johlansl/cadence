#!/bin/sh
# test-agent-readiness.sh -- fixture tests for check-agent-host-upgrade-readiness.sh.
#
# Each case builds a fake host root (binaries, agent.env) plus a fake
# systemctl(1) emitting canned `show` output, then asserts the exit status
# and key report lines. No root, no systemd, no network required.

set -eu

HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
CHECK=$HERE/check-agent-host-upgrade-readiness.sh

passed=0
failed=0

ok() { printf '  ok   %s\n' "$1"; passed=$((passed + 1)); }
fail() { printf '  FAIL %s\n  %s\n' "$1" "$2"; failed=$((failed + 1)); }

# new_case <name> -- sets T (scratch), ROOT (fake /), FAKEBIN (fake systemctl).
new_case() {
	T=$(mktemp -d)
	ROOT=$T/root
	FAKEBIN=$T/bin/systemctl
	mkdir -p "$ROOT/usr/bin" "$ROOT/usr/local/bin" "$ROOT/etc/cadence" "$T/bin" "$T/show"
	cat >"$FAKEBIN" <<'EOF'
#!/bin/sh
# Fake systemctl: `show ... -- <unit>` prints the canned file, anything
# else fails. Reads $FIXTURE_SHOW_DIR.
for last in "$@"; do :; done
file=$FIXTURE_SHOW_DIR/$last
if [ -f "$file" ]; then cat "$file"; else echo "LoadState=not-found"; fi
EOF
	chmod +x "$FAKEBIN"
}

# fake_binary <path> <version> -- sh stand-in printing a version.
fake_binary() {
	printf '#!/bin/sh\necho %s\n' "$2" >"$1"
	chmod 755 "$1"
}

# show_svc <unit> <fragment> <dropins> <exepath> -- canned service output.
show_svc() {
	cat >"$T/show/$1" <<EOF
LoadState=loaded
FragmentPath=$2
DropInPaths=$3
ExecStart={ path=$4 ; argv[]=$4 ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }
EOF
}

show_timer() {
	printf 'LoadState=loaded\nUnitFileState=%s\n' "$2" >"$T/show/$1"
}

# standard conforming services+timers; override fragment dir per case.
conforming_shows() {
	fragdir=$1
	for svc in cadence-agent.service cadence-agent-poll.service cadence-agent-health-check-boot.service; do
		show_svc "$svc" "$fragdir/$svc" "" "/usr/bin/cadence-agent"
	done
	for tmr in cadence-agent.timer cadence-agent-poll.timer cadence-agent-health-check-boot.timer; do
		show_timer "$tmr" enabled
	done
}

run_check() {
	set +e # the check is *expected* to fail on most fixtures
	FIXTURE_SHOW_DIR=$T/show CADENCE_CHECK_ROOT=$ROOT SYSTEMCTL=$FAKEBIN \
		sh "$CHECK" >"$T/out" 2>&1
	echo $? >"$T/exit"
	set -e
}

expect_exit() {
	name=$1; want=$2
	got=$(cat "$T/exit")
	if [ "$got" = "$want" ]; then ok "$name (exit $got)"; else fail "$name" "exit $got, want $want:"; sed 's/^/    /' "$T/out"; fi
}

expect_grep() {
	name=$1; pattern=$2
	if grep -q "$pattern" "$T/out"; then ok "$name"; else fail "$name" "missing /$pattern/:"; sed 's/^/    /' "$T/out"; fi
}

cleanup() { rm -rf "$T"; }

# --- 1. conforming via /etc units -------------------------------------------------
new_case
fake_binary "$ROOT/usr/bin/cadence-agent" 0.15.0
printf 'CADENCE_TOKEN=x\n' >"$ROOT/etc/cadence/agent.env"
chmod 600 "$ROOT/etc/cadence/agent.env"
conforming_shows /etc/systemd/system
run_check
expect_exit conforming-etc 0
expect_grep conforming-etc-ready '6B-READY'
cleanup

# --- 2. conforming via /usr/lib (.deb pur) -----------------------------------------
new_case
fake_binary "$ROOT/usr/bin/cadence-agent" 0.15.0
printf 'CADENCE_TOKEN=x\n' >"$ROOT/etc/cadence/agent.env"
chmod 600 "$ROOT/etc/cadence/agent.env"
conforming_shows /usr/lib/systemd/system
run_check
expect_exit conforming-lib 0
cleanup

# --- 3. old /usr/local ExecStart ----------------------------------------------------
new_case
fake_binary "$ROOT/usr/bin/cadence-agent" 0.15.0
fake_binary "$ROOT/usr/local/bin/cadence-agent" 0.14.0
printf 'CADENCE_TOKEN=x\n' >"$ROOT/etc/cadence/agent.env"
chmod 600 "$ROOT/etc/cadence/agent.env"
for svc in cadence-agent.service cadence-agent-poll.service cadence-agent-health-check-boot.service; do
	show_svc "$svc" "/etc/systemd/system/$svc" "" "/usr/local/bin/cadence-agent"
done
for tmr in cadence-agent.timer cadence-agent-poll.timer cadence-agent-health-check-boot.timer; do
	show_timer "$tmr" enabled
done
run_check
expect_exit legacy-execstart 1
expect_grep legacy-execstart-names 'NOT 6B-READY'
expect_grep legacy-execstart-legacy '/usr/local/bin/cadence-agent'
expect_grep legacy-execstart-nodelete 'do not delete'
cleanup

# --- 4. mixed: /usr/bin binary but stale /etc fragment masking ----------------------
new_case
fake_binary "$ROOT/usr/bin/cadence-agent" 0.15.0
printf 'CADENCE_TOKEN=x\n' >"$ROOT/etc/cadence/agent.env"
chmod 600 "$ROOT/etc/cadence/agent.env"
show_svc cadence-agent.service /etc/systemd/system/cadence-agent.service "" /usr/local/bin/cadence-agent
show_svc cadence-agent-poll.service /usr/lib/systemd/system/cadence-agent-poll.service "" /usr/bin/cadence-agent
show_svc cadence-agent-health-check-boot.service /usr/lib/systemd/system/cadence-agent-health-check-boot.service "" /usr/bin/cadence-agent
for tmr in cadence-agent.timer cadence-agent-poll.timer cadence-agent-health-check-boot.timer; do
	show_timer "$tmr" enabled
done
run_check
expect_exit mixed-one-stale 1
expect_grep mixed-fragment '/etc/systemd/system/cadence-agent.service'
cleanup

# --- 5. custom drop-in (fail-closed, even without legacy content) --------------------
new_case
fake_binary "$ROOT/usr/bin/cadence-agent" 0.15.0
printf 'CADENCE_TOKEN=x\n' >"$ROOT/etc/cadence/agent.env"
chmod 600 "$ROOT/etc/cadence/agent.env"
mkdir -p "$ROOT/etc/systemd/system/cadence-agent.service.d"
printf '[Service]\nNice=5\n' >"$ROOT/etc/systemd/system/cadence-agent.service.d/override.conf"
show_svc cadence-agent.service /etc/systemd/system/cadence-agent.service \
	"$ROOT/etc/systemd/system/cadence-agent.service.d/override.conf" /usr/bin/cadence-agent
show_svc cadence-agent-poll.service /etc/systemd/system/cadence-agent-poll.service "" /usr/bin/cadence-agent
show_svc cadence-agent-health-check-boot.service /etc/systemd/system/cadence-agent-health-check-boot.service "" /usr/bin/cadence-agent
for tmr in cadence-agent.timer cadence-agent-poll.timer cadence-agent-health-check-boot.timer; do
	show_timer "$tmr" enabled
done
run_check
expect_exit dropin-custom 1
expect_grep dropin-names 'drop-ins'
cleanup

# --- 6. canonical binary missing ------------------------------------------------------
new_case
printf 'CADENCE_TOKEN=x\n' >"$ROOT/etc/cadence/agent.env"
chmod 600 "$ROOT/etc/cadence/agent.env"
conforming_shows /etc/systemd/system
run_check
expect_exit canonical-missing 1
expect_grep canonical-missing-msg 'is missing'
cleanup

# --- 7. canonical path is a symlink ----------------------------------------------------
new_case
fake_binary "$ROOT/usr/bin/real-agent" 0.15.0
ln -s real-agent "$ROOT/usr/bin/cadence-agent"
printf 'CADENCE_TOKEN=x\n' >"$ROOT/etc/cadence/agent.env"
chmod 600 "$ROOT/etc/cadence/agent.env"
conforming_shows /etc/systemd/system
run_check
expect_exit canonical-symlink 1
expect_grep canonical-symlink-msg 'symlink'
cleanup

# --- 8. unreferenced residue: ready with warning ----------------------------------------
new_case
fake_binary "$ROOT/usr/bin/cadence-agent" 0.15.0
fake_binary "$ROOT/usr/local/bin/cadence-agent" 0.14.0
printf 'CADENCE_TOKEN=x\n' >"$ROOT/etc/cadence/agent.env"
chmod 600 "$ROOT/etc/cadence/agent.env"
conforming_shows /etc/systemd/system
run_check
expect_exit residue-unreferenced 0
expect_grep residue-warn 'unreferenced'
cleanup

# --- 9. agent.env missing ----------------------------------------------------------------
new_case
fake_binary "$ROOT/usr/bin/cadence-agent" 0.15.0
conforming_shows /etc/systemd/system
run_check
expect_exit agent-env-missing 1
expect_grep agent-env-msg 'not enrolled'
cleanup

# --- 10. canonical not executable ----------------------------------------------------------
new_case
fake_binary "$ROOT/usr/bin/cadence-agent" 0.15.0
chmod 644 "$ROOT/usr/bin/cadence-agent"
printf 'CADENCE_TOKEN=x\n' >"$ROOT/etc/cadence/agent.env"
chmod 600 "$ROOT/etc/cadence/agent.env"
conforming_shows /etc/systemd/system
run_check
expect_exit canonical-notexec 1
expect_grep canonical-notexec-msg 'not executable'
cleanup

# --- 11. timer not enabled -------------------------------------------------------------------
new_case
fake_binary "$ROOT/usr/bin/cadence-agent" 0.15.0
printf 'CADENCE_TOKEN=x\n' >"$ROOT/etc/cadence/agent.env"
chmod 600 "$ROOT/etc/cadence/agent.env"
for svc in cadence-agent.service cadence-agent-poll.service cadence-agent-health-check-boot.service; do
	show_svc "$svc" "/etc/systemd/system/$svc" "" "/usr/bin/cadence-agent"
done
show_timer cadence-agent.timer enabled
show_timer cadence-agent-poll.timer disabled
show_timer cadence-agent-health-check-boot.timer enabled
run_check
expect_exit timer-disabled 1
expect_grep timer-msg 'not enabled'
cleanup

# --- 12. bad -version output -----------------------------------------------------------------
new_case
fake_binary "$ROOT/usr/bin/cadence-agent" 'bogus!!'
printf 'CADENCE_TOKEN=x\n' >"$ROOT/etc/cadence/agent.env"
chmod 600 "$ROOT/etc/cadence/agent.env"
conforming_shows /etc/systemd/system
run_check
expect_exit bad-version 1
expect_grep bad-version-msg 'not strict N.N.N'
cleanup

# --- 13. systemctl unavailable ----------------------------------------------------------------
new_case
fake_binary "$ROOT/usr/bin/cadence-agent" 0.15.0
set +e # expected failure
CADENCE_CHECK_ROOT=$ROOT SYSTEMCTL=$T/bin/no-such-binary \
	sh "$CHECK" >"$T/out" 2>&1
echo $? >"$T/exit"
set -e
expect_exit no-systemctl 1
expect_grep no-systemctl-msg 'NOT READY'
cleanup

# --- 14. unit not loaded -----------------------------------------------------------------------
new_case
fake_binary "$ROOT/usr/bin/cadence-agent" 0.15.0
printf 'CADENCE_TOKEN=x\n' >"$ROOT/etc/cadence/agent.env"
chmod 600 "$ROOT/etc/cadence/agent.env"
printf 'LoadState=not-found\n' >"$T/show/cadence-agent-poll.service"
show_svc cadence-agent.service /etc/systemd/system/cadence-agent.service "" /usr/bin/cadence-agent
show_svc cadence-agent-health-check-boot.service /etc/systemd/system/cadence-agent-health-check-boot.service "" /usr/bin/cadence-agent
for tmr in cadence-agent.timer cadence-agent-poll.timer cadence-agent-health-check-boot.timer; do
	show_timer "$tmr" enabled
done
run_check
expect_exit unit-notfound 1
expect_grep unit-notfound-msg 'not loaded'
cleanup

printf '\nreadiness-test: %d passed, %d failed\n' "$passed" "$failed"
[ "$failed" -eq 0 ]
