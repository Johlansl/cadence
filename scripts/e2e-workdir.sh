# shellcheck shell=sh
# e2e-workdir.sh -- isolation helpers of scripts/test-agent-upgrade-e2e.sh.
#
# Sourced, never executed: functions only. Kept apart from the E2E script so
# the safety logic has its own fast test (scripts/test-e2e-workdir.sh) with a
# fake docker, instead of being exercised only by a 35 minute privileged run.
#
# Every function prints the reason on stderr and returns 1 when it refuses.

E2E_MARKER=.e2e-copy

# e2e_free_kb <path>: free KiB on the filesystem holding <path> (the nearest
# existing, readable parent when the path itself is not).
e2e_free_kb() {
	_p=$1
	while :; do
		if [ -d "$_p" ] && _o=$(df -Pk "$_p" 2>/dev/null); then
			printf '%s\n' "$_o" | awk 'NR==2 {print $4}'
			return 0
		fi
		[ "$_p" = "/" ] && return 1
		_p=$(dirname -- "$_p")
	done
}

# e2e_check_free_space <path> <min KiB> <what>
e2e_check_free_space() {
	if ! _free=$(e2e_free_kb "$1") || [ -z "$_free" ]; then
		echo "e2e: cannot read the free space of $3 ($1): refusing" >&2
		return 1
	fi
	if [ "$_free" -lt "$2" ]; then
		echo "e2e: only $((_free / 1024)) MiB free on $3 ($1), need $(($2 / 1024)) MiB: refusing" >&2
		return 1
	fi
}

# e2e_make_copy <origin checkout> <destination dir>
# Copies what git would commit or show as untracked, ignored files excluded:
# the real .env, dist/ contents and backups/ stay behind. Writes the marker
# that tells the E2E script it is running in a copy, and makes sure the copy
# has no .env of its own.
e2e_make_copy() {
	if ! git -C "$1" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
		echo "e2e: $1 is not a git work tree: nothing to copy from, refusing" >&2
		return 1
	fi
	_list=$(mktemp) || return 1
	(cd "$1" && git -c core.quotepath=off ls-files --cached --others --exclude-standard) |
		while IFS= read -r _f; do
			[ -e "$1/$_f" ] && printf '%s\n' "$_f"
		done >"$_list"
	if grep -q '^"' "$_list"; then
		echo "e2e: a file name needs git quoting: refusing to copy" >&2
		rm -f -- "$_list"
		return 1
	fi
	mkdir -p "$2" || return 1
	if ! (cd "$1" && tar -cf - -T "$_list") | tar -xf - -C "$2"; then
		echo "e2e: copying the checkout failed" >&2
		rm -f -- "$_list"
		return 1
	fi
	rm -f -- "$_list"
	rm -f -- "$2/.env"
	mkdir -p "$2/dist"
	printf 'origin=%s\n' "$1" >"$2/$E2E_MARKER"
}

# e2e_refuse_if_mounted <dir>: refuse when a running container bind-mounts
# <dir> or anything below it (the production caddy and backend mount ./dist
# of their checkout). Fails closed when docker cannot be asked.
e2e_refuse_if_mounted() {
	if ! _ids=$(docker ps -q 2>/dev/null); then
		echo "e2e: cannot list running containers: refusing" >&2
		return 1
	fi
	_ifs=$IFS
	for _id in $_ids; do
		if ! _srcs=$(docker inspect --format '{{range .Mounts}}{{println .Source}}{{end}}' "$_id" 2>/dev/null); then
			echo "e2e: cannot inspect container $_id: refusing" >&2
			return 1
		fi
		IFS='
'
		for _s in $_srcs; do
			case $_s in
			"$1" | "$1"/*)
				IFS=$_ifs
				echo "e2e: a running container ($_id) mounts $_s, inside $1: refusing" >&2
				return 1
				;;
			esac
		done
		IFS=$_ifs
	done
}

# e2e_check_no_leftovers: no container, network or volume whose name starts
# with "e2e" (previous run, or something else using the namespace). Images
# are not checked: e2e-backend, e2e-frontend and e2e-host are this test's own
# build cache and are rebuilt every run.
e2e_check_no_leftovers() {
	_found=""
	_a=$(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -E '^e2e' || true)
	_b=$(docker network ls --format '{{.Name}}' 2>/dev/null | grep -E '^e2e' || true)
	_c=$(docker volume ls --format '{{.Name}}' 2>/dev/null | grep -E '^e2e' || true)
	[ -n "$_a" ] && _found="$_found containers: $(printf '%s' "$_a" | tr '\n' ' ')"
	[ -n "$_b" ] && _found="$_found networks: $(printf '%s' "$_b" | tr '\n' ' ')"
	[ -n "$_c" ] && _found="$_found volumes: $(printf '%s' "$_c" | tr '\n' ' ')"
	if [ -n "$_found" ]; then
		echo "e2e: resources named e2e* already exist:$_found" >&2
		echo "e2e: remove them yourself if they are stale (docker rm -f / network rm / volume rm): refusing" >&2
		return 1
	fi
}

# e2e_prod_snapshot <origin dir>: a text block describing everything the test
# must not change: every running container that is not an e2e one (id, start
# time, image id), the two production image tags, and the content hash of the
# origin's ./dist. Compare the block before and after the run.
e2e_prod_snapshot() {
	docker ps --format '{{.Names}}' 2>/dev/null | grep -v '^e2e' | sort |
		while IFS= read -r _n; do
			docker inspect --format 'container {{.Name}} {{.Id}} {{.State.StartedAt}} image={{.Image}}' "$_n" 2>&1
		done
	for _t in cadence-backend:latest cadence-frontend:latest; do
		docker image inspect --format "tag $_t {{.Id}}" "$_t" 2>/dev/null || echo "tag $_t absent"
	done
	if [ -d "$1/dist" ]; then
		_h=$(cd "$1/dist" && find . -type f | sort | while IFS= read -r _f; do sha256sum "$_f"; done | sha256sum | cut -d' ' -f1)
		echo "dist $_h"
	else
		echo "dist absent"
	fi
}

# e2e_launch <origin checkout> <script path relative to the checkout>
# The launcher half of the E2E script: makes the copy in $TMPDIR, runs the
# script from it (which then finds the marker and does the real work), removes
# the copy afterwards and returns the script's exit status.
E2E_COPY_DIR=""
e2e_launch_cleanup() {
	case $E2E_COPY_DIR in
	*/cadence-e2e-copy.*)
		rm -rf -- "$E2E_COPY_DIR" || echo "e2e: could not remove $E2E_COPY_DIR, remove it yourself" >&2
		;;
	esac
	E2E_COPY_DIR=""
	return 0
}
e2e_launch() {
	_tmp=${TMPDIR:-/tmp}
	e2e_check_free_space "$_tmp" 1048576 "the work directory" || return 1
	E2E_COPY_DIR=$(mktemp -d "$_tmp/cadence-e2e-copy.XXXXXX") || return 1
	trap 'e2e_launch_cleanup' EXIT
	trap 'e2e_launch_cleanup; exit 130' INT
	trap 'e2e_launch_cleanup; exit 143' TERM
	if ! e2e_make_copy "$1" "$E2E_COPY_DIR"; then
		e2e_launch_cleanup
		return 1
	fi
	echo "e2e: running from a throwaway copy of $1 at $E2E_COPY_DIR"
	_rc=0
	sh "$E2E_COPY_DIR/$2" || _rc=$?
	e2e_launch_cleanup
	return "$_rc"
}
