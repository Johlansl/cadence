#!/bin/sh
# check-agent-host-upgrade-readiness.sh -- read-only 6B readiness probe.
#
# Answers one question: is this host proven to run the Cadence agent from
# the canonical path (/usr/bin/cadence-agent) through its EFFECTIVE systemd
# configuration? A host is 6B-ready only if every agent unit that can launch
# the binary effectively points at the canonical path.
#
# Read-only: inspects files and `systemctl show`, writes nothing, needs no
# network, never prints secrets (agent.env presence/mode only).
#
#   exit 0 -- host is 6B-ready (warnings allowed, listed)
#   exit 1 -- host is NOT ready; failures listed, migrate first
#
# Test seams (production leaves them unset):
#   CADENCE_CHECK_ROOT=/path  prefix filesystem paths (fixtures)
#   SYSTEMCTL=/path/to/fake  replace the systemctl binary (fixtures)
#
# This is the operator-side half of the 6B path migration (runbook:
# docs/agent-path-migration.md). It qualifies hosts BEFORE any real
# agent_upgrade job may target them.

set -eu

ROOT=${CADENCE_CHECK_ROOT:-/}
SYSTEMCTL=${SYSTEMCTL:-systemctl}
CANON_BIN=/usr/bin/cadence-agent
LEGACY_BIN=/usr/local/bin/cadence-agent
ENV_FILE=/etc/cadence/agent.env

fails=0
warns=0

ok()   { printf '  [ok]   %s\n' "$1"; }
fail() { printf '  [FAIL] %s\n' "$1"; fails=$((fails + 1)); }
warn() { printf '  [warn] %s\n' "$1"; warns=$((warns + 1)); }
info() { printf '         %s\n' "$1"; }

prefix() {
	# Join $ROOT and an absolute path without producing a double slash
	# that would confuse humans reading the report.
	case $ROOT in
	/) printf '%s' "$1" ;;
	*) printf '%s%s' "${ROOT%/}" "$1" ;;
	esac
}

if ! command -v "$SYSTEMCTL" >/dev/null 2>&1 && [ ! -x "$SYSTEMCTL" ]; then
	printf 'check-agent-host-upgrade-readiness: cannot run %s\n' "$SYSTEMCTL" >&2
	printf 'Effective systemd state is unprovable without it: NOT READY.\n' >&2
	exit 1
fi

printf 'Cadence agent 6B readiness (effective systemd state)\n\n'

# --- 1. Canonical binary -------------------------------------------------
printf 'binary:\n'
canon=$(prefix "$CANON_BIN")
if [ -L "$canon" ]; then
	fail "$CANON_BIN is a symlink (the agent resolves it and refuses unless it lands exactly here)"
elif [ ! -f "$canon" ]; then
	fail "$CANON_BIN is missing"
else
	if [ -x "$canon" ]; then
		ok "$CANON_BIN exists, regular file, executable"
	else
		fail "$CANON_BIN is not executable"
	fi
	if command -v stat >/dev/null 2>&1; then
		mode=$(stat -c %a "$canon" 2>/dev/null || echo '?')
		owner=$(stat -c %u "$canon" 2>/dev/null || echo '?')
		case $mode in
		755) ok "mode 0755" ;;
		?) warn "mode unreadable (stat failed)" ;;
		*) fail "mode is $mode, want 0755" ;;
		esac
		case $owner in
		0) ok "owned by root" ;;
		?) warn "owner unreadable (stat failed)" ;;
		*) warn "owned by uid $owner, want root (informational)" ;;
		esac
	else
		warn "stat unavailable: mode/owner unchecked"
	fi
	if ver=$("$canon" -version 2>/dev/null); then
		case $ver in
		'' | *[!0-9.]* | *.*.*.*)
			fail "-version output is not strict N.N.N: $ver" ;;
		*.*.*)
			v1=${ver%%.*}; rest=${ver#*.}
			v2=${rest%%.*}; v3=${rest#*.}
			case $v3 in *.*) fail "-version output is not N.N.N: $ver" ;;
			*)
				if [ -n "$v1" ] && [ -n "$v2" ] && [ -n "$v3" ]; then
					ok "reports version $ver"
				else
					fail "-version output is not N.N.N: $ver"
				fi
				;;
			esac
			;;
		*) fail "-version output is not N.N.N: $ver" ;;
		esac
	else
		fail "$CANON_BIN -version does not run"
	fi
fi

# --- 2. Effective service configuration ----------------------------------
printf 'services (effective):\n'
show_have_legacy_ref=0
for svc in cadence-agent.service cadence-agent-poll.service cadence-agent-health-check-boot.service; do
	show=$("$SYSTEMCTL" show -p LoadState,FragmentPath,DropInPaths,ExecStart -- "$svc" 2>/dev/null || true)
	load=$(printf '%s\n' "$show" | sed -n 's/^LoadState=//p')
	if [ "$load" != "loaded" ]; then
		fail "$svc is not loaded (LoadState=$load)"
		continue
	fi
	frag=$(printf '%s\n' "$show" | sed -n 's/^FragmentPath=//p')
	dropins=$(printf '%s\n' "$show" | sed -n 's/^DropInPaths=//p' | tr ' ' '\n' | grep . || true)
	execs=$(printf '%s\n' "$show" | sed -n 's/^ExecStart=.*path=\([^ ;}]*\).*/\1/p')
	if [ -z "$execs" ]; then
		fail "$svc has no parsable effective ExecStart (fragment $frag)"
		continue
	fi
	svc_ok=1
	# shellcheck disable=SC2086 # splitting the ExecStart list is intended
	for exe in $execs; do
		case $exe in
		"$CANON_BIN") : ;;
		*)
			fail "$svc effective ExecStart runs $exe (fragment $frag)"
			svc_ok=0
			;;
		esac
	done
	if [ -n "$dropins" ]; then
		fail "$svc carries custom drop-ins (operator review required):"
		# shellcheck disable=SC2086 # splitting the drop-in list is intended
		for d in $dropins; do
			info "$d"
			if [ -f "$d" ] && grep -q "$LEGACY_BIN" "$d" 2>/dev/null; then
				show_have_legacy_ref=1
			fi
		done
		svc_ok=0
	fi
	if [ "$svc_ok" = 1 ]; then
		ok "$svc runs $CANON_BIN (fragment $frag)"
	fi
	case " $execs " in
	*" $LEGACY_BIN "*) show_have_legacy_ref=1 ;;
	esac
done

# --- 3. Timers ------------------------------------------------------------
printf 'timers:\n'
for tmr in cadence-agent.timer cadence-agent-poll.timer cadence-agent-health-check-boot.timer; do
	show=$("$SYSTEMCTL" show -p LoadState,UnitFileState -- "$tmr" 2>/dev/null || true)
	load=$(printf '%s\n' "$show" | sed -n 's/^LoadState=//p')
	state=$(printf '%s\n' "$show" | sed -n 's/^UnitFileState=//p')
	if [ "$load" != "loaded" ]; then
		fail "$tmr is not loaded (LoadState=$load)"
	elif [ "$state" != "enabled" ]; then
		fail "$tmr is not enabled (UnitFileState=$state)"
	else
		ok "$tmr loaded and enabled"
	fi
done

# --- 4. Legacy residue ------------------------------------------------------
printf 'legacy residue:\n'
legacy=$(prefix "$LEGACY_BIN")
if [ ! -e "$legacy" ]; then
	ok "$LEGACY_BIN absent"
elif [ "$show_have_legacy_ref" = 1 ]; then
	fail "$LEGACY_BIN exists AND is still referenced by effective config (do not delete, migrate first)"
else
	warn "$LEGACY_BIN exists but is unreferenced (safe to remove manually after validation; see runbook)"
fi

# --- 5. Enrollment identity (presence/mode only, never contents) ------------
printf 'identity:\n'
envf=$(prefix "$ENV_FILE")
if [ ! -f "$envf" ]; then
	fail "$ENV_FILE is missing (host not enrolled)"
else
	ok "$ENV_FILE present (contents never displayed)"
	if command -v stat >/dev/null 2>&1; then
		mode=$(stat -c %a "$envf" 2>/dev/null || echo '?')
		case $mode in
		600) ok "mode 0600" ;;
		?) warn "mode unreadable (stat failed)" ;;
		*) warn "mode is $mode, want 0600 (informational)" ;;
		esac
	fi
fi

# --- verdict -----------------------------------------------------------------
printf '\n'
if [ "$fails" -gt 0 ]; then
	printf 'NOT 6B-READY: %d failure(s), %d warning(s). Migrate per docs/agent-path-migration.md.\n' "$fails" "$warns"
	exit 1
fi
printf '6B-READY: canonical path proven effective (%d warning(s)).\n' "$warns"
exit 0
