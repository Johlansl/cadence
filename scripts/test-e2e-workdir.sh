#!/bin/sh
# test-e2e-workdir.sh -- tests for the isolation logic of the agent upgrade
# E2E (scripts/e2e-workdir.sh, plus static checks of the E2E script and its
# compose override). A fake docker and a fake df stand in for the real ones:
# nothing is started, nothing is built, no real .env or ./dist is read.
#
#   sh scripts/test-e2e-workdir.sh

set -eu

HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH='' cd -- "$HERE/.." && pwd)
E2E=$HERE/test-agent-upgrade-e2e.sh

command -v git >/dev/null 2>&1 || { echo "e2e-workdir-test: git is required" >&2; exit 1; }

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
export TMPDIR="$T/tmp"
mkdir -p "$TMPDIR"

# shellcheck source=scripts/e2e-workdir.sh
. "$HERE/e2e-workdir.sh"

# --- fakes -------------------------------------------------------------------------
FAKE=$T/fake
mkdir -p "$FAKE" "$T/bin"
export FAKE
cat >"$T/bin/docker" <<'EOF'
#!/bin/sh
# fake docker: state lives in files under $FAKE; every call is logged
echo "$*" >>"$FAKE/calls.log"
[ -f "$FAKE/docker.broken" ] && exit 1
for last; do :; done
case "$1" in
ps)
	case " $* " in
	*" -a "*) cat "$FAKE/ps_all" 2>/dev/null || true ;;
	*" -q "*) awk '{print $1}' "$FAKE/ps" 2>/dev/null || true ;;
	*) awk '{print $2}' "$FAKE/ps" 2>/dev/null || true ;;
	esac
	;;
inspect)
	case "$*" in
	*"range .Mounts"*) cat "$FAKE/mounts.$last" 2>/dev/null || true ;;
	*) cat "$FAKE/inspect.$last" ;;
	esac
	;;
info) echo "$FAKE/dockerroot" ;;
network) cat "$FAKE/networks" 2>/dev/null || true ;;
volume) cat "$FAKE/volumes" 2>/dev/null || true ;;
image)
	[ -f "$FAKE/image.$last" ] || exit 1
	echo "tag $last $(cat "$FAKE/image.$last")"
	;;
*) exit 1 ;;
esac
EOF
cat >"$T/bin/df" <<'EOF'
#!/bin/sh
# fake df -Pk <path>: free space from $FAKE/df_free_kb; fails for $FAKE/noaccess
for last; do :; done
[ "$last" = "$FAKE/noaccess" ] && exit 1
[ -f "$FAKE/df.broken" ] && exit 1
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "fake 100000000 1 $(cat "$FAKE/df_free_kb") 1% /"
EOF
chmod +x "$T/bin/docker" "$T/bin/df"
PATH="$T/bin:$PATH"

reset_fake() {
	rm -rf "$FAKE"
	mkdir -p "$FAKE"
	: >"$FAKE/ps"
	: >"$FAKE/ps_all"
	: >"$FAKE/networks"
	: >"$FAKE/volumes"
	echo 100000000 >"$FAKE/df_free_kb"
}

# --- 1. the copy -------------------------------------------------------------------------
echo "1. e2e_make_copy"
SENTINEL='SENTINEL-d3adb33f-never-copy-this'
O=$T/origin
mkdir -p "$O/scripts" "$O/dist" "$O/backups/b1"
(
	cd "$O"
	git init -q
	printf '.env\ndist/*\n!dist/.gitkeep\nbackups/\n' >.gitignore
	echo tracked >tracked.txt
	echo x >scripts/x.sh
	echo gone >gone.txt
	: >dist/.gitkeep
	echo "a b" >"a b.txt"
	git add -A
	git -c user.email=t@t -c user.name=t commit -qm init
	rm gone.txt
	echo untracked >untracked.txt
	echo "CADENCE_ADMIN_KEY=$SENTINEL" >.env
	echo "$SENTINEL" >dist/served.bin
	echo "CADENCE_ADMIN_KEY=$SENTINEL" >backups/b1/env
)
C=$T/copy
RC=0
e2e_make_copy "$O" "$C" >"$T/out" 2>&1 || RC=$?
if [ "$RC" -eq 0 ]; then ok "copy made (exit=$RC)"; else fail "copy failed (exit=$RC): $(cat "$T/out")"; fi
for f in tracked.txt scripts/x.sh untracked.txt "a b.txt" dist/.gitkeep; do
	if [ -f "$C/$f" ]; then ok "copied: $f"; else fail "missing in the copy: $f"; fi
done
if [ ! -e "$C/gone.txt" ]; then ok "a tracked file deleted from the work tree is skipped"; else fail "gone.txt copied"; fi
if [ ! -e "$C/.env" ]; then ok "the ignored .env is not copied"; else fail ".env copied"; fi
if [ ! -e "$C/dist/served.bin" ] && [ ! -e "$C/backups" ]; then ok "ignored dist/ content and backups/ are not copied"; else fail "dist content or backups copied"; fi
if grep -rqF "$SENTINEL" "$C"; then fail "a real secret value is somewhere in the copy"; else ok "no secret value anywhere in the copy"; fi
if [ "$(cat "$C/$E2E_MARKER")" = "origin=$O" ]; then ok "the marker names the origin"; else fail "marker: $(cat "$C/$E2E_MARKER")"; fi
if [ -d "$C/dist" ]; then ok "dist/ exists, empty of served files"; else fail "no dist/ in the copy"; fi

# a .env committed by mistake is not carried into the copy either
O2=$T/origin2
mkdir -p "$O2"
(cd "$O2" && git init -q && echo "K=$SENTINEL" >.env && git add -A && git -c user.email=t@t -c user.name=t commit -qm env)
RC=0
e2e_make_copy "$O2" "$T/copy2" >"$T/out" 2>&1 || RC=$?
if [ "$RC" -eq 0 ] && [ ! -e "$T/copy2/.env" ]; then ok "a tracked .env is dropped from the copy"; else fail "tracked .env (exit=$RC)"; fi

RC=0
mkdir -p "$T/notgit"
e2e_make_copy "$T/notgit" "$T/copy3" >"$T/out" 2>&1 || RC=$?
if [ "$RC" -ne 0 ] && grep -q "not a git work tree" "$T/out" && [ ! -e "$T/copy3" ]; then ok "a directory that is not a git work tree is refused, nothing created (exit=$RC)"; else fail "non-git origin (exit=$RC)"; fi

# --- 2. refuse when a running container mounts the directory ---------------------------
echo "2. e2e_refuse_if_mounted"
reset_fake
printf 'aaa111 cadence-caddy-1\nbbb222 cadence-db-1\n' >"$FAKE/ps"
printf '/home/x/cadence/dist\n/home/x/cadence/Caddyfile\n' >"$FAKE/mounts.aaa111"
printf '/var/lib/docker/volumes/pg\n' >"$FAKE/mounts.bbb222"
RC=0
e2e_refuse_if_mounted /home/x/cadence >"$T/out" 2>&1 || RC=$?
if [ "$RC" -ne 0 ] && grep -q "aaa111" "$T/out"; then ok "a container mounting <dir>/dist makes it refuse, naming the container (exit=$RC)"; else fail "mounted dist not detected (exit=$RC)"; fi
RC=0
e2e_refuse_if_mounted /home/x/other >"$T/out" 2>&1 || RC=$?
if [ "$RC" -eq 0 ]; then ok "an unrelated directory is accepted (exit=$RC)"; else fail "false refusal: $(cat "$T/out")"; fi
RC=0
e2e_refuse_if_mounted /home/x/cadence/dist >"$T/out" 2>&1 || RC=$?
if [ "$RC" -ne 0 ]; then ok "the mounted directory itself is refused (exit=$RC)"; else fail "exact mount not detected"; fi
RC=0
e2e_refuse_if_mounted /home/x/cade >"$T/out" 2>&1 || RC=$?
if [ "$RC" -eq 0 ]; then ok "a name prefix is not a path prefix (/home/x/cade vs /home/x/cadence)"; else fail "prefix confusion"; fi
: >"$FAKE/ps"
RC=0
e2e_refuse_if_mounted /home/x/cadence >"$T/out" 2>&1 || RC=$?
if [ "$RC" -eq 0 ]; then ok "no running container: accepted (exit=$RC)"; else fail "no container: $(cat "$T/out")"; fi
: >"$FAKE/docker.broken"
RC=0
e2e_refuse_if_mounted /home/x/cadence >"$T/out" 2>&1 || RC=$?
if [ "$RC" -ne 0 ] && grep -q "cannot list" "$T/out"; then ok "docker unavailable: fails closed (exit=$RC)"; else fail "docker down not refused (exit=$RC)"; fi

# --- 3. no e2e* leftovers ---------------------------------------------------------------------
echo "3. e2e_check_no_leftovers"
reset_fake
printf 'cadence-db-1\ncadence-caddy-1\n' >"$FAKE/ps_all"
printf 'bridge\ncadence_default\n' >"$FAKE/networks"
printf 'cadence_pgdata\n8bfb0ace90ac8a142009135e20858ee2e284f0e65fe32c2872b7ea0ae68f0f69\n' >"$FAKE/volumes"
RC=0
e2e_check_no_leftovers >"$T/out" 2>&1 || RC=$?
if [ "$RC" -eq 0 ]; then ok "production names and an anonymous volume that merely contains 'e2e' are accepted (exit=$RC)"; else fail "false positive: $(cat "$T/out")"; fi
printf 'e2e-host-a\n' >>"$FAKE/ps_all"
RC=0
e2e_check_no_leftovers >"$T/out" 2>&1 || RC=$?
if [ "$RC" -ne 0 ] && grep -q "e2e-host-a" "$T/out"; then ok "a leftover e2e container is refused (exit=$RC)"; else fail "container leftover (exit=$RC)"; fi
printf 'cadence-db-1\n' >"$FAKE/ps_all"
printf 'e2e_default\n' >>"$FAKE/networks"
RC=0
e2e_check_no_leftovers >"$T/out" 2>&1 || RC=$?
if [ "$RC" -ne 0 ] && grep -q "e2e_default" "$T/out"; then ok "a leftover e2e network is refused (exit=$RC)"; else fail "network leftover (exit=$RC)"; fi
printf 'bridge\n' >"$FAKE/networks"
printf 'e2e_pgdata\n' >>"$FAKE/volumes"
RC=0
e2e_check_no_leftovers >"$T/out" 2>&1 || RC=$?
if [ "$RC" -ne 0 ] && grep -q "e2e_pgdata" "$T/out"; then ok "a leftover e2e volume is refused (exit=$RC)"; else fail "volume leftover (exit=$RC)"; fi

# --- 4. free space ---------------------------------------------------------------------------
echo "4. free space"
reset_fake
echo 10000000 >"$FAKE/df_free_kb"
RC=0
e2e_check_free_space "$T" 4194304 "the Docker filesystem" >"$T/out" 2>&1 || RC=$?
if [ "$RC" -eq 0 ]; then ok "about 9.5 GiB free, 4 GiB needed: accepted (exit=$RC)"; else fail "enough space refused: $(cat "$T/out")"; fi
echo 641000 >"$FAKE/df_free_kb"
RC=0
e2e_check_free_space "$T" 4194304 "the Docker filesystem" >"$T/out" 2>&1 || RC=$?
if [ "$RC" -ne 0 ] && grep -q "only 625 MiB free" "$T/out"; then ok "641 MB free (the real figure that triggered this) is refused with the numbers (exit=$RC)"; else fail "low space (exit=$RC): $(cat "$T/out")"; fi
mkdir -p "$FAKE/noaccess"
echo 10000000 >"$FAKE/df_free_kb"
RC=0
e2e_check_free_space "$FAKE/noaccess/sub/dir" 4194304 "x" >"$T/out" 2>&1 || RC=$?
if [ "$RC" -eq 0 ]; then ok "an unreadable or missing path falls back to the nearest readable parent (exit=$RC)"; else fail "fallback: $(cat "$T/out")"; fi
: >"$FAKE/df.broken"
RC=0
e2e_check_free_space "$T" 4194304 "x" >"$T/out" 2>&1 || RC=$?
if [ "$RC" -ne 0 ]; then ok "df unusable: fails closed (exit=$RC)"; else fail "df broken not refused"; fi

# --- 5. the proof of non-impact -----------------------------------------------------------------
echo "5. e2e_prod_snapshot"
reset_fake
printf 'id1 cadence-backend-1\nid2 cadence-frontend-1\nid3 e2e-db-1\n' >"$FAKE/ps"
echo "container /cadence-backend-1 FULLID1 2026-10-10T10:00:00Z image=f546" >"$FAKE/inspect.cadence-backend-1"
echo "container /cadence-frontend-1 FULLID2 2026-10-10T10:00:01Z image=ab95" >"$FAKE/inspect.cadence-frontend-1"
echo "container /e2e-db-1 SHOULDNOTAPPEAR 2026-10-10T11:00:00Z image=zzz" >"$FAKE/inspect.e2e-db-1"
echo f546full >"$FAKE/image.cadence-backend:latest"
echo b078full >"$FAKE/image.cadence-frontend:latest"
mkdir -p "$T/live/dist"
echo served >"$T/live/dist/a.bin"
S1=$(e2e_prod_snapshot "$T/live")
S2=$(e2e_prod_snapshot "$T/live")
if [ "$S1" = "$S2" ]; then ok "an unchanged production gives an identical snapshot"; else fail "snapshot not stable"; fi
if printf '%s\n' "$S1" | grep -q "e2e-db-1" ; then fail "an e2e container is part of the snapshot"; else ok "e2e containers are left out of the snapshot"; fi
if printf '%s\n' "$S1" | grep -q "cadence-frontend:latest b078full"; then ok "the cadence-frontend tag is in the snapshot"; else fail "frontend tag missing: $S1"; fi
echo "container /cadence-backend-1 FULLID9 2026-10-10T10:00:00Z image=f546" >"$FAKE/inspect.cadence-backend-1"
if [ "$(e2e_prod_snapshot "$T/live")" != "$S1" ]; then ok "a recreated production container changes the snapshot"; else fail "container change not detected"; fi
echo "container /cadence-backend-1 FULLID1 2026-10-10T10:00:00Z image=f546" >"$FAKE/inspect.cadence-backend-1"
echo e2efull >"$FAKE/image.cadence-backend:latest"
if [ "$(e2e_prod_snapshot "$T/live")" != "$S1" ]; then ok "a moved cadence-backend tag changes the snapshot"; else fail "backend tag move not detected"; fi
echo f546full >"$FAKE/image.cadence-backend:latest"
echo b078NEW >"$FAKE/image.cadence-frontend:latest"
if [ "$(e2e_prod_snapshot "$T/live")" != "$S1" ]; then ok "a moved cadence-frontend tag changes the snapshot"; else fail "frontend tag move not detected"; fi
echo b078full >"$FAKE/image.cadence-frontend:latest"
echo more >"$T/live/dist/b.bin"
if [ "$(e2e_prod_snapshot "$T/live")" != "$S1" ]; then ok "a changed ./dist changes the snapshot"; else fail "dist change not detected"; fi
rm -f "$T/live/dist/b.bin"
if [ "$(e2e_prod_snapshot "$T/live")" = "$S1" ]; then ok "restoring everything gives the original snapshot back"; else fail "snapshot not restored"; fi

# --- 6. the launcher -------------------------------------------------------------------------------
echo "6. e2e_launch"
reset_fake
L=$T/lorigin
mkdir -p "$L/scripts"
(
	cd "$L"
	git init -q
	printf '.env\n' >.gitignore
	cat >scripts/stub.sh <<'EOF'
#!/bin/sh
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH='' cd -- "$here/.." && pwd)
{
	echo "marker=$(test -f "$repo/.e2e-copy" && echo yes || echo no)"
	echo "repo=$repo"
	echo "env_here=$(test -e "$repo/.env" && echo yes || echo no)"
} >"$STUB_OUT"
exit 7
EOF
	git add -A
	git -c user.email=t@t -c user.name=t commit -qm stub
	echo "K=$SENTINEL" >.env
)
STUB_OUT="$T/stub.out"
export STUB_OUT
RC=0
( e2e_launch "$L" scripts/stub.sh ) >"$T/out" 2>&1 || RC=$?
if [ "$RC" -eq 7 ]; then ok "the launcher returns the script's exit status (exit=$RC)"; else fail "launcher exit=$RC: $(cat "$T/out")"; fi
if grep -q '^marker=yes' "$STUB_OUT" && ! grep -q "^repo=$L\$" "$STUB_OUT"; then ok "the script ran from a copy (marker present, not in the origin)"; else fail "ran in the origin? $(cat "$STUB_OUT")"; fi
if grep -q '^env_here=no' "$STUB_OUT"; then ok "the copy has no .env"; else fail "a .env was in the copy"; fi
if [ -z "$(find "$TMPDIR" -maxdepth 1 -name 'cadence-e2e-copy*')" ]; then ok "the copy is removed afterwards"; else fail "copy left behind: $(ls -A "$TMPDIR")"; fi
if [ ! -e "$L/.e2e-copy" ] && [ "$(git -C "$L" status --short | tr -d ' ')" = "" ]; then ok "the origin checkout was not touched"; else fail "origin modified: $(git -C "$L" status --short)"; fi

echo 100 >"$FAKE/df_free_kb"
rm -f "$STUB_OUT"
RC=0
( e2e_launch "$L" scripts/stub.sh ) >"$T/out" 2>&1 || RC=$?
if [ "$RC" -ne 0 ] && [ ! -e "$STUB_OUT" ] && grep -q "refusing" "$T/out"; then ok "no disk space: refused before copying or running anything (exit=$RC)"; else fail "low disk launch (exit=$RC)"; fi
echo 100000000 >"$FAKE/df_free_kb"

RC=0
( e2e_launch "$T/notgit" scripts/stub.sh ) >"$T/out" 2>&1 || RC=$?
if [ "$RC" -ne 0 ] && [ -z "$(find "$TMPDIR" -maxdepth 1 -name 'cadence-e2e-copy*')" ]; then ok "a non-git origin is refused and leaves no temp directory (exit=$RC)"; else fail "non-git launch (exit=$RC)"; fi

# --- 7. static guarantees of the real script and its compose override -----------------------
echo "7. the E2E script and its compose override"
if grep -qF ". \"\$here/e2e-workdir.sh\"" "$E2E" && grep -qF "e2e_launch \"\$repo\"" "$E2E"; then ok "the E2E script sources the library and starts with the launcher"; else fail "launcher missing from the E2E script"; fi
if grep -qF -- "--env-file \$repo/.env" "$E2E" && ! grep -qF 'e2e.env' "$E2E"; then ok "compose reads the disposable .env of the copy, no other env file"; else fail "env file wiring"; fi
for line in CADENCE_ADVISORY_REFRESH_ENABLED=false CADENCE_CVE_SCORE_REFRESH_ENABLED=false CADENCE_NVD_API_KEY= \
	CADENCE_ADVISORY_FEED_URLS= CADENCE_OIDC_ENABLED=false CADENCE_OIDC_ISSUER= CADENCE_WEBHOOK_ALLOW_PRIVATE_IPS=false; do
	if grep -qx "$line" "$E2E"; then ok "disposable .env sets $line"; else fail "disposable .env lacks $line"; fi
done
if grep -q 'e2e_refuse_if_mounted' "$E2E" && grep -q 'e2e_check_no_leftovers' "$E2E" && grep -q 'e2e_check_free_space' "$E2E" && grep -q 'e2e_prod_snapshot' "$E2E"; then ok "the preflight uses all four guards and the final proof"; else fail "a guard is not wired into the E2E script"; fi
COMPOSE_E2E=$REPO/docker-compose.e2e.yml
if awk '/^  [a-z_]+:$/ {svc=$1} /^    image: e2e-backend$/ {if (svc == "backend:") b = 1; if (svc == "scheduler:") s = 1; n++} END {exit (b && s && n == 2) ? 0 : 1}' "$COMPOSE_E2E"; then
	ok "the compose override renames exactly the backend and scheduler images to e2e-backend"
else
	fail "backend/scheduler image override missing or misplaced"
fi
if grep -v '^[[:space:]]*#' "$COMPOSE_E2E" | grep -q 'cadence-'; then fail "the override sets a production-looking name"; else ok "no production name is set by the override (comments aside)"; fi

# --- 8. a failure before phase 2: the cleanup is still complete ---------------------------------
# Phase 2 is where the script writes its .env. Before that, every compose call
# fails ("couldn't find env file"); the cleanup must absorb it and still remove
# the host containers, try the compose teardown, and delete its temp directory,
# and the script must not have created a .env on the way. The real script is
# run, in a fake copy, against a fake docker that says a container mounts the
# copy: the preflight guard then stops it before phase 2.
echo "8. cleanup after a failure before phase 2"
if command -v minisign >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1 && command -v openssl >/dev/null 2>&1 && command -v curl >/dev/null 2>&1; then
	reset_fake
	C8=$T/copy8
	mkdir -p "$C8/scripts" "$C8/dist" "$T/tmp8"
	cp "$E2E" "$HERE/e2e-workdir.sh" "$C8/scripts/"
	printf 'origin=%s\n' "$T/nowhere" >"$C8/$E2E_MARKER"
	C8REAL=$(CDPATH='' cd -- "$C8" && pwd -P)
	printf 'aaa111 cadence-caddy-1\n' >"$FAKE/ps"
	printf '%s/dist\n' "$C8REAL" >"$FAKE/mounts.aaa111"
	FAIL_DIRS_BEFORE=$(find /tmp -maxdepth 1 -name 'e2e-fail-*' 2>/dev/null | sort)
	RC=0
	(cd "$C8" && TMPDIR="$T/tmp8" sh scripts/test-agent-upgrade-e2e.sh) >"$T/out8" 2>&1 || RC=$?
	# the failure path keeps its diagnostics in /tmp by design: remove only what this run created
	for d in $(find /tmp -maxdepth 1 -name 'e2e-fail-*' 2>/dev/null | sort); do
		case "
$FAIL_DIRS_BEFORE
" in
		*"
$d
"*) ;;
		*) rm -rf -- "$d" ;;
		esac
	done
	if [ "$RC" -eq 1 ]; then ok "the script stops with exit 1 at the preflight (exit=$RC)"; else fail "exit=$RC: $(tail -5 "$T/out8")"; fi
	if [ "$(uname -s)" = "Linux" ] && [ "$(uname -m)" = "x86_64" ]; then
		if grep -q "a running container mounts this directory" "$T/out8"; then ok "it was stopped by the mounted-directory guard"; else fail "not stopped by the guard: $(tail -5 "$T/out8")"; fi
	fi
	if [ ! -e "$C8/.env" ]; then ok "no .env was created before phase 2"; else fail "a .env exists in the copy"; fi
	if grep -q '^rm -f e2e-host-a e2e-host-b$' "$FAKE/calls.log"; then ok "cleanup: the host containers are removed"; else fail "cleanup skipped the host containers: $(cat "$FAKE/calls.log")"; fi
	if grep -q '^compose -p e2e .* down -v$' "$FAKE/calls.log"; then ok "cleanup: the compose teardown is attempted although it fails without the .env"; else fail "cleanup skipped the compose teardown"; fi
	if [ -z "$(ls -A "$T/tmp8")" ]; then ok "cleanup: the script's own temp directory is removed"; else fail "temp leftovers: $(ls -A "$T/tmp8")"; fi
	if grep -q "cleaning up" "$T/out8"; then ok "the cleanup ran to its end message"; else fail "no cleanup message"; fi
else
	echo "  skip cleanup test: needs minisign, python3, openssl and curl"
fi

echo "e2e-workdir-test: $pass passed, $failed failed"
[ "$failed" -eq 0 ]
