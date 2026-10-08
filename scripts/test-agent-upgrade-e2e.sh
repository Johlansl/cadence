#!/bin/sh
# test-agent-upgrade-e2e.sh -- real end-to-end proof of one agent upgrade.
#
# Stands up a throwaway Cadence stack (compose project `e2e`, fresh DB,
# test secrets) plus privileged systemd host containers, then drives the
# full N -> N+1 chain with production code paths only:
#
#   enroll (real) -> timers (real) -> publish (real PR2 workflow, test key)
#   -> job (real API) -> claim by timer -> verified install -> rename
#   -> next timer runs N+1 -> server-side proof -> succeeded
#
# plus recovery-after-timeout, offline-host, pre-commit-failure and
# backend-restart scenarios. One command, self-cleaning:
#
#   sh scripts/test-agent-upgrade-e2e.sh
#
# Needs: docker (privileged containers allowed), sh, git, minisign,
# python3, curl, openssl, linux/amd64 (the release build smoke), and free
# loopback ports 5433/8001/8081/8082/4443/8444. Takes ~35 minutes, mostly
# real 60s timer cycles and one real 420s proof timeout. No production
# secret is read or written; ./dist is snapshotted and restored.
#
# Verdict: exit 0 prints PRIMITIVE UNITAIRE VALIDEE POUR ORCHESTRATION,
# anything else prints PRIMITIVE NON VALIDEE plus the failed assertion.

set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH='' cd -- "$here/.." && pwd)
cd "$repo"

V_N=90.0.1
V_NEXT=90.0.2
V_BRICK=90.0.3
V_TAMP=90.0.4
V_OFF=90.0.5
V_CAMP=90.0.6
V_BLAST=90.0.7
ALL_VERS="$V_N $V_NEXT $V_BRICK $V_TAMP $V_OFF $V_CAMP $V_BLAST"

T=$(mktemp -d)
trap 'e2e_cleanup' EXIT HUP INT TERM

PASS=0
log()  { printf '[e2e %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
pass() { PASS=$((PASS + 1)); log "PASS[$PASS] $*"; }
die() { # die MSG -- log, persist diagnostics outside $T (trap deletes it), exit 1
	log "FAIL: $*"
	D=$(mktemp -d /tmp/e2e-fail-XXXXXX)
	{
		echo "### fail: $*"
		echo '### docker ps'; docker ps -a 2>&1
		echo '### compose services'; docker compose -p e2e ps 2>&1
	} >"$D/host.txt" 2>&1 || true
	${COMPOSE:-false} logs --no-log-prefix --tail=60 backend scheduler caddy db \
		>"$D/compose-logs.txt" 2>&1 || true
	for h in e2e-host-a e2e-host-b; do
		if docker exec "$h" true 2>/dev/null; then
			{
				echo "### $h timers"; docker exec "$h" systemctl list-timers --no-pager 2>&1
				echo "### $h poll service"; docker exec "$h" systemctl status cadence-agent-poll.service --no-pager 2>&1
				echo "### $h poll journal"; docker exec "$h" journalctl -u cadence-agent-poll.service --no-pager 2>&1 | tail -40
				echo "### $h full journal tail"; docker exec "$h" journalctl --no-pager 2>&1 | tail -20
				echo "### $h agent.env keys"; docker exec "$h" sh -c 'grep -o "^[A-Z_]*=" /etc/cadence/agent.env 2>&1'
				echo "### $h version"; docker exec "$h" /usr/bin/cadence-agent -version 2>&1
			} >"$D/$h.txt" 2>&1 || true
		fi
	done
	[ -n "${PG_PASSWORD:-}" ] && PGPASSWORD=$PG_PASSWORD docker exec e2e-db-1 psql \
		-U "${PG_USER:-e2e}" -d "${PG_DB:-e2e}" -c \
		'SELECT id,hostname,agent_version,last_seen_at FROM hosts' \
		-c 'SELECT id,job_type,status FROM jobs' \
		-c 'SELECT id,name,status,job_type,halt_reason FROM campaigns' >"$D/db.txt" 2>&1 || true
	cp "$T"/*.log "$D/" 2>/dev/null || true
	log "diagnostics persisted in $D"
	log 'PRIMITIVE NON VALIDEE'
	exit 1
}
need() { command -v "$1" >/dev/null 2>&1 || die "missing tool: $1"; }

e2e_cleanup() {
	trap - EXIT HUP INT TERM
	log 'cleaning up...'
	docker rm -f e2e-host-a e2e-host-b >/dev/null 2>&1 || true
	# shellcheck disable=SC2086 # word-splitting $COMPOSE is intended
	${COMPOSE:-false} down -v >/dev/null 2>&1 || true
	if [ -d "$T/dist-backup" ]; then
		rm -rf "$repo/dist"
		mv "$T/dist-backup" "$repo/dist"
	fi
	rm -rf "$T"
}

COMPOSE="docker compose -p e2e -f $repo/docker-compose.yml -f $repo/docker-compose.e2e.yml --env-file $T/e2e.env"

# --- phase 0: preflight -------------------------------------------------------
log 'phase 0/11: preflight'
need docker; need git; need minisign; need python3; need curl; need openssl
[ "$(uname -s)" = Linux ] && [ "$(uname -m)" = "x86_64" ] || die 'need linux/amd64'
docker info >/dev/null 2>&1 || die 'docker unreachable'
for p in 5433 8001 8081 8082 4443 8444; do
	if python3 -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1',$p)) == 0 else 1)"; then
		die "loopback port $p is busy (stop whatever holds it)"
	fi
done
case $COMPOSE in
*'-p e2e'*) ;;
*) die 'COMPOSE lost its isolated project name' ;;
esac
# Static self-check: every compose invocation must carry the isolated
# project (the PR8 incident was a bare `up` recreating the dev stack).
if grep -n 'docker compose' "$0" | grep -v -- '-p e2e' | grep -q .; then
	die 'a compose call bypasses the -p e2e isolation'
fi
# Snapshot a pre-existing dev stack, if any: the final phase proves the
# run never recreated it (same container id + start time).
if docker inspect cadence-backend-1 >/dev/null 2>&1; then
	DEV_BACKEND=$(docker inspect --format '{{.Id}}{{.State.StartedAt}}' cadence-backend-1)
else
	DEV_BACKEND=none
fi
pass 'preflight (tools, platform, ports free, compose isolated)'

# --- helpers ------------------------------------------------------------------
api() { # api METHOD PATH [JSON] -> body; dies on transport/HTTP error
	m=$1; p=$2; body=${3:-}
	if [ -n "$body" ]; then
		curl -fsSL -m 30 -X "$m" "http://127.0.0.1:8001$p" \
			-H 'Content-Type: application/json' -H "X-Admin-Key: $ADMIN_KEY" \
			-d "$body" || die "API $m $p failed"
	else
		curl -fsSL -m 30 -X "$m" "http://127.0.0.1:8001$p" \
			-H "X-Admin-Key: $ADMIN_KEY" || die "API $m $p failed"
	fi
}

api_code() { # api_code POST PATH JSON -> prints "CODE BODY" (never dies on HTTP code)
	# Unique tmp file per call: recovery/offline scenarios call make_job
	# in parallel, and a shared body file lets one subshell read the
	# other's response (wrong job id, phantom timeouts). mktemp, not
	# $$: $$ is the main shell PID even inside subshells.
	bf=$(mktemp "$T/api-body-XXXXXX.tmp")
	out=$(curl -sS -m 30 -o "$bf" -w '%{http_code}' -X POST \
		"http://127.0.0.1:8001$2" -H 'Content-Type: application/json' \
		-H "X-Admin-Key: $ADMIN_KEY" -d "$3" 2>/dev/null) \
		|| die "API POST $2 transport failed"
	printf '%s %s' "$out" "$(cat "$bf")"
	rm -f "$bf"
}

psql_e2e() { # psql_e2e SQL -> single value
	PGPASSWORD=$PG_PASSWORD docker exec e2e-db-1 psql -U "$PG_USER" -d "$PG_DB" \
		-tAX -c "$1" || die "psql failed: $1"
}

hexec() { docker exec "$@" || die "docker exec $* failed"; }

wait_until() { # wait_until DESC TIMEOUT CMD... -- polls every 5s
	desc=$1; timeout=$2; shift 2 # CMD runs in THIS shell: functions allowed
	deadline=$(( $(date +%s) + timeout ))
	while [ "$(date +%s)" -lt "$deadline" ]; do
		if "$@" >/dev/null 2>&1; then return 0; fi
		sleep 5
	done
	die "timed out waiting: $desc"
}

probe_job_status() { [ "$(job_field "$1" status)" = "$2" ]; } # JOBID WANT
probe_agent_version() { # HOSTID WANT
	[ "$(psql_e2e "SELECT agent_version FROM hosts WHERE id = '$1'")" = "$2" ]
}
probe_systemd() { docker exec "$1" systemctl is-system-running 2>/dev/null | grep -qE '^(running|degraded)$'; }
probe_canonical_version() { [ "$(host_version "$1" 2>/dev/null)" = "$2" ]; } # CTR WANT
probe_url() { curl -fsSL -m 10 "$1" -o /dev/null; }

job_field() { psql_e2e "SELECT $2 FROM jobs WHERE id = '$1'"; }

host_version() { hexec "$1" /usr/bin/cadence-agent -version; }

snapshot_creds() { # snapshot_creds CONTAINER OUTFILE (hashes only)
	hexec "$1" sh -c 'sha256sum /etc/cadence/*; stat -c "%n %a %u" /etc/cadence/*' >"$2"
}

# --- phase 1: test key, release repo, seven signed bundles ----------------------
log 'phase 1/11: test key + release builds (real PR2 workflow)'
minisign -G -W -s "$T/e2e-minisign.key" -p "$T/e2e-minisign.pub" >/dev/null 2>&1 \
	|| die 'minisign -G failed'
chmod 600 "$T/e2e-minisign.key"
RREL=$T/release-repo
mkdir -p "$RREL"
cp -r "$repo/agent" "$RREL/agent"
rm -rf "$RREL/agent/bin" # local build outputs are gitignored for real; keep the tag clean
mkdir -p "$RREL/agent/bin" # user-owned dir: docker-as-root drops files we can still delete
# The whole test fleet (N included) must embed the TEST root of trust,
# exactly as a production fleet embeds the production one: swap both the
# committed pubkey file and the compiled literal, keeping them in sync.
cp "$T/e2e-minisign.pub" "$RREL/agent/minisign.pub"
python3 - "$RREL/agent/internal/upgrade/trust.go" "$T/e2e-minisign.pub" <<'EOF' \
	|| die 'test-key embed patch failed'
import re, sys
dst, pub = sys.argv[1], sys.argv[2]
comment, key = open(pub).read().strip().split('\n')
new = 'const trustedPubKeyText = `%s\\n%s`' % (comment, key)
src = open(dst).read()
src, n = re.subn(r'const trustedPubKeyText = `[^`]*`', new, src)
assert n == 1, 'const block not found'
open(dst, 'w').write(src)
EOF
{
	echo '# Agent changelog (E2E fixture headings)'
	for v in $ALL_VERS; do echo; echo "## $v"; echo; echo "- E2E test release."; done
} >"$RREL/agent/CHANGELOG.md"
(cd "$RREL" && git init -q && git add -A && git -c user.email=e2e -c user.name=e2e commit -qm e2e \
	&& for v in $ALL_VERS; do git tag "agent-v$v"; done) \
	|| die 'release repo setup failed'
for v in $ALL_VERS; do
	log "building release $v"
	CADENCE_RELEASE_REPO=$RREL CADENCE_SIGNING_KEY=$T/e2e-minisign.key \
		CADENCE_RELEASE_PUBKEY=$T/e2e-minisign.pub \
		sh "$repo/scripts/build-agent-release.sh" "$v" "$T/bundle-$v" >/dev/null \
		|| die "build-agent-release.sh $v failed"
	test -x "$T/bundle-$v/cadence-agent-linux-amd64" || die "bundle $v missing amd64"
	rm -f "$RREL/agent/bin"/* # keep the next tag-discipline check clean
done
pass 'seven key-holder builds, signed with the test key (server never saw it)'

# --- phase 2: throwaway stack ---------------------------------------------------
log 'phase 2/11: starting throwaway stack'
PG_USER=e2e; PG_PASSWORD=$(openssl rand -hex 16); PG_DB=e2e
ADMIN_KEY=$(openssl rand -hex 24)
FERNET=$(python3 -c 'import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')
PROXY_KEY=$(openssl rand -hex 24)
cat >"$T/e2e.env" <<EOF
POSTGRES_USER=$PG_USER
POSTGRES_PASSWORD=$PG_PASSWORD
POSTGRES_DB=$PG_DB
CADENCE_ADMIN_KEY=$ADMIN_KEY
CADENCE_TOKEN_ENCRYPTION_KEY=$FERNET
CADENCE_SITE_ADDRESS=caddy
CADENCE_AGENT_PORT=8443
CADENCE_LEGACY_AGENT_ENDPOINTS=off
CADENCE_INTERNAL_PROXY_KEY=$PROXY_KEY
CADENCE_DASHBOARD_AUTH=off
CADENCE_UPGRADE_PROOF_TIMEOUT_SECONDS=420
EOF
# shellcheck disable=SC2086 # word-splitting $COMPOSE is intended
$COMPOSE up -d --build >/dev/null || die 'compose up failed'
wait_until 'backend healthy' 240 curl -fsSL -m 3 'http://127.0.0.1:8001/readyz'
# shellcheck disable=SC2016 # the $() expands in the inner sh -c, not here
wait_until 'scheduler healthy' 240 sh -c \
	'test "$(docker inspect --format "{{.State.Health.Status}}" e2e-scheduler-1)" = healthy'
pass 'stack healthy (db, backend, scheduler, caddy)'

# Snapshot ./dist BEFORE the E2E writes anything there; restored by trap.
cp -a "$repo/dist" "$T/dist-backup"
CAROOT=$(docker exec e2e-caddy-1 sh -c 'find /data -name root.crt | head -1')
[ -n "$CAROOT" ] || die 'caddy internal root not found'
docker cp "e2e-caddy-1:$CAROOT" "$T/e2e-ca.crt"
mkdir -p "$repo/dist/agent"
cp "$T/e2e-ca.crt" "$repo/dist/agent/ca.crt" # what `publish ... assets` would stage
grep -q 'BEGIN CERTIFICATE' "$T/e2e-ca.crt" || die 'bad CA extract'
pass 'test CA extracted (backend enrollment + agent trust share it)'

api POST /api/v1/admin/webhooks \
	'{"url":"http://127.0.0.1:9/e2e","event_types":["job.succeeded","job.failed","campaign.completed","campaign.stopped"],"description":"e2e"}' \
	>/dev/null
pass 'webhook subscription created (undeliverable URL: rows stage anyway)'

# --- phase 3: systemd hosts, real enrollment ------------------------------------
log 'phase 3/11: host containers + enrollment'
docker build -q -t e2e-host -f "$repo/scripts/e2e-host.Dockerfile" "$repo/scripts" >/dev/null \
	|| die 'host image build failed'

setup_host() { # setup_host NAME VERSION -- container, install, enroll, timers, readiness
	name=$1; ver=$2
	docker run -d --name "$name" --hostname "$name" --privileged --cgroupns=host \
		-v /sys/fs/cgroup:/sys/fs/cgroup:rw -e container=docker \
		--network e2e_default e2e-host >/dev/null || die "$name: run failed"
	wait_until "$name systemd running" 120 probe_systemd "$name"
	docker cp "$T/bundle-$ver/cadence-agent-linux-amd64" "$name:/usr/bin/cadence-agent" \
		|| die "$name: binary install failed"
	hexec "$name" chmod 0755 /usr/bin/cadence-agent
	for u in cadence-agent.service cadence-agent.timer cadence-agent-poll.service \
		cadence-agent-poll.timer cadence-agent-health-check-boot.service \
		cadence-agent-health-check-boot.timer; do
		docker cp "$repo/agent/systemd/$u" "$name:/etc/systemd/system/$u" \
			|| die "$name: unit $u install failed"
	done
	hexec "$name" mkdir -p /etc/cadence
	docker cp "$T/e2e-ca.crt" "$name:/etc/cadence/server-ca.crt" \
		|| die "$name: CA transfer failed"
	code=$(api POST /api/v1/admin/enrollments \
		"{\"expected_hostname\":\"$name\",\"label\":\"e2e\",\"ttl_minutes\":30}" \
		| python3 -c 'import json,sys; print(json.load(sys.stdin)["code"])')
	[ -n "$code" ] || die "$name: empty enrollment code"
	printf '%s\n' "$code" | docker exec -i "$name" /usr/bin/cadence-agent \
		-enroll -enroll-server https://caddy -enroll-ca /etc/cadence/server-ca.crt \
		-enroll-directory /etc/cadence || die "$name: enroll failed"
	hexec "$name" systemctl daemon-reload
	hexec "$name" systemctl enable --now cadence-agent.timer \
		cadence-agent-poll.timer cadence-agent-health-check-boot.timer
	docker cp "$repo/scripts/check-agent-host-upgrade-readiness.sh" "$name:/root/readiness.sh"
	hexec "$name" sh /root/readiness.sh | tee "$T/readiness-$name.txt" | grep -q 6B-READY \
		|| die "$name: not 6B-READY after install (see $T/readiness-$name.txt)"
	pass "$name enrolled, timers enabled, readiness 6B-READY"
}

(trap - EXIT HUP INT TERM; setup_host e2e-host-a "$V_N" >"$T/setup-a.log" 2>&1) &
PID_A=$!
(trap - EXIT HUP INT TERM; setup_host e2e-host-b "$V_N" >"$T/setup-b.log" 2>&1) &
PID_B=$!
wait $PID_A || die 'host-a setup failed (see setup-a.log)'
wait $PID_B || die 'host-b setup failed (see setup-b.log)'
pass 'both hosts set up in parallel'

host_id() { # host_id HOSTNAME -> uuid
	api GET "/api/v1/hosts" | python3 -c "
import json,sys
for h in json.load(sys.stdin):
    if h['hostname'] == '$1':
        print(h['id'])
" | grep . || die "host $1 unknown to the server"
}

A_ID=$(host_id e2e-host-a)
B_ID=$(host_id e2e-host-b)
log "host ids: a=$A_ID b=$B_ID"

# First authenticated polls (OnBootSec=1min): agent_version must flow via poll.
wait_until 'host-a first poll' 180 probe_agent_version "$A_ID" "$V_N"
wait_until 'host-b first poll' 180 probe_agent_version "$B_ID" "$V_N"
pass 'both hosts polling with agent_version over mTLS'

snapshot_creds e2e-host-a "$T/creds-a-before.txt"
sha256sum "$T/bundle-$V_N/cadence-agent-linux-amd64" | awk '{print $1}' >"$T/hash-n.txt"
sha256sum "$T/bundle-$V_NEXT/cadence-agent-linux-amd64" | awk '{print $1}' >"$T/hash-n1.txt"
sha256sum "$T/bundle-$V_BRICK/cadence-agent-linux-amd64" | awk '{print $1}' >"$T/hash-n2.txt"
hexec e2e-host-a cat /proc/sys/kernel/random/boot_id >"$T/boot-a.txt"
pass 'baselines captured (hashes, credentials, boot id)'

publish() { # publish VERSION -- real PR2 workflow, test key, server-side stage
	log "publishing agent $1"
	CADENCE_PUBLISH_PUBKEY=$T/e2e-minisign.pub \
		sh "$repo/scripts/publish-agent.sh" publish "$1" "$T/bundle-$1" >/dev/null \
		|| die "publish $1 failed"
	curl -fsSL -m 30 --cacert "$T/e2e-ca.crt" --resolve caddy:4443:127.0.0.1 \
		"https://caddy:4443/agent/v$1/cadence-agent-linux-amd64" -o "$T/served-$1" \
		|| die "published $1 not served on :443"
	[ "$(sha256sum "$T/served-$1" | awk '{print $1}')" = \
		"$(sha256sum "$T/bundle-$1/cadence-agent-linux-amd64" | awk '{print $1}')" ] \
		|| die "served bytes differ from bundle for $1"
}

assert_key_absent_from_servers() {
	for c in e2e-backend-1 e2e-scheduler-1 e2e-caddy-1 e2e-db-1; do
		found=$(docker exec "$c" sh -c 'find / -name "*e2e-minisign*" 2>/dev/null' || true)
		[ -z "$found" ] || die "test key material inside $c: $found"
	done
	[ -z "$(find "$repo/dist" -name '*.key' 2>/dev/null)" ] \
		|| die 'private key file under ./dist'
}

make_job() { # make_job HOSTID TARGET -> prints job id; asserts audit+pending+params
	payload="{\"job_type\":\"agent_upgrade\",\"params\":{\"target_version\":\"$2\"}}"
	deadline=$(( $(date +%s) + 180 )) # a boot health_check may hold the guard briefly
	while :; do
		resp=$(api_code POST "/api/v1/admin/hosts/$1/jobs" "$payload")
		code=${resp%% *}; body=${resp#* }
		case $code in
		201) break ;;
		409)
			[ "$(date +%s)" -lt "$deadline" ] || die "host $1 never free for job"
			sleep 5
			;;
		*) die "job create: HTTP $code $body" ;;
		esac
	done
	jid=$(printf '%s' "$body" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
	[ "$(job_field "$jid" status)" = pending ] || die "job $jid not pending"
	[ "$(job_field "$jid" job_type)" = agent_upgrade ] || die "job $jid wrong type"
	[ "$(job_field "$jid" "params->>'target_version'")" = "$2" ] || die "job $jid bad target"
	[ "$(psql_e2e "SELECT count(*) FROM jsonb_object_keys((SELECT params FROM jobs WHERE id = '$jid'))")" = 1 ] \
		|| die "job $jid params not exactly {target_version}"
	n=$(psql_e2e "SELECT count(*) FROM audit_log WHERE action = 'job.create' AND target_id = '$jid'")
	[ "$n" = 1 ] || die "job $jid audit rows: $n"
	printf '%s' "$jid"
}

# --- phase 4: happy path N -> N+1 --------------------------------------------------
log 'phase 4/11: happy path (publish, claim by timer, install, proof)'
publish "$V_NEXT"
assert_key_absent_from_servers
pass 'N+1 published via PR2 workflow; served bytes match; test key never on servers'

J1=$(make_job "$A_ID" "$V_NEXT")
log "job $J1 created, waiting for timer claim"
T0=$(date +%s)
wait_until 'claim (job running)' 240 probe_job_status "$J1" running
T_RUNNING=$(date +%s)
pass "claimed by real timer after $((T_RUNNING - T0))s (job running)"
wait_until 'rename to N+1' 300 probe_canonical_version e2e-host-a "$V_NEXT"
T_RENAME=$(date +%s)
[ $((T_RENAME - T_RUNNING)) -lt 300 ] || die 'rename exceeded the local deadline'
pass "renamed to $V_NEXT $((T_RENAME - T_RUNNING))s after claim (inside 300s)"

# Backend restart between rename and proof: no process memory may matter.
# shellcheck disable=SC2086 # word-splitting $COMPOSE is intended
$COMPOSE restart backend >/dev/null || die 'backend restart failed'
wait_until 'backend healthy again' 180 probe_url 'http://127.0.0.1:8001/readyz'
[ "$(job_field "$J1" status)" = running ] || die 'job left running across backend restart'
pass 'backend restarted mid-flight; job still running (PostgreSQL is the truth)'

wait_until 'server-side proof (job succeeded)' 240 probe_job_status "$J1" succeeded
T_OK=$(date +%s)
[ -n "$(job_field "$J1" completed_at)" ] || die 'succeeded without completed_at'
[ "$(job_field "$J1" "result->>'proven'")" = true ] || die "result.proven not true"
[ "$(psql_e2e "SELECT count(*) FROM jsonb_object_keys((SELECT result FROM jobs WHERE id = '$J1'))")" = 1 ] \
	|| die 'result is not exactly {proven}'
sleep 30 # let the dispatcher settle, then count terminal events
n_ok=$(psql_e2e "SELECT count(*) FROM webhook_deliveries WHERE event_type = 'job.succeeded' AND payload->'data'->>'job_id' = '$J1'")
n_bad=$(psql_e2e "SELECT count(*) FROM webhook_deliveries WHERE event_type = 'job.failed' AND payload->'data'->>'job_id' = '$J1'")
[ "$n_ok" = 1 ] && [ "$n_bad" = 0 ] || die "deliveries for $J1: ok=$n_ok failed=$n_bad"
pass "job succeeded by PR4 proof after $((T_OK - T_RENAME))s; result proven-only; exactly one succeeded event"
pass 'N submitted no succeeded (result is the server-made proven blob)'

# No restart, ever: same boot, timer-triggered runs, distinct PIDs.
[ "$(hexec e2e-host-a cat /proc/sys/kernel/random/boot_id)" = "$(cat "$T/boot-a.txt")" ] \
	|| die 'host rebooted mid-scenario'
hexec e2e-host-a journalctl -u cadence-agent-poll.service -o json --no-pager >"$T/poll-journal.json"
python3 - "$T/poll-journal.json" <<'EOF' || die 'restart/PID proof failed'
import json, sys
pids = set()
starts = 0
for line in open(sys.argv[1]):
    try:
        e = json.loads(line)
    except ValueError:
        continue
    msg = e.get('MESSAGE', '')
    # Idle polls stay quiet by design, so agent PIDs are not enumerable
    # here; the distinct-process proof is run 1 (binary N, it renamed) plus
    # an authenticated N+1 contact afterwards (only N+1 bytes emit that).
    if 'Starting' in msg or 'Finished' in msg:
        starts += 1
    assert 'restart' not in msg.lower(), 'restart mentioned in poll journal'
assert starts >= 4, f'expected 2+ timer runs, saw {starts} start/finish lines'
print(f'{starts // 2} timer runs, no restart')
EOF
pass 'no restart: same boot id, 2+ timer runs, N exited and N+1 authenticated fresh'

# Installed state + identity.
[ "$(hexec e2e-host-a sha256sum /usr/bin/cadence-agent | awk '{print $1}')" = "$(cat "$T/hash-n1.txt")" ] \
	|| die 'canonical is not the published N+1 bytes'
[ "$(hexec e2e-host-a stat -c %a /usr/bin/cadence-agent)" = 755 ] || die 'canonical mode != 0755'
[ "$(hexec e2e-host-a sha256sum /usr/bin/cadence-agent.prev | awk '{print $1}')" = "$(cat "$T/hash-n.txt")" ] \
	|| die '.prev is not N'
[ "$(hexec e2e-host-a /usr/bin/cadence-agent.prev -version)" = "$V_N" ] || die '.prev -version != N'
pass 'canonical=N+1 (0755), .prev=N (hash + -version)'
snapshot_creds e2e-host-a "$T/creds-a-after.txt"
cmp -s "$T/creds-a-before.txt" "$T/creds-a-after.txt" || die 'credentials changed during upgrade'
pass 'credentials/config identical (hashes + modes)'

# No secret may leak into backend logs or the host journal.
TOKEN=$(hexec e2e-host-a sh -c 'grep ^CADENCE_TOKEN= /etc/cadence/agent.env | cut -d= -f2-')
KEYLINE=$(hexec e2e-host-a sh -c 'grep -v "^---" /etc/cadence/client-*.key | head -1 | cut -c1-40')
[ -n "$TOKEN" ] && [ -n "$KEYLINE" ] || die 'cannot read test credentials for leak check'
# shellcheck disable=SC2086 # word-splitting $COMPOSE is intended
$COMPOSE logs --no-log-prefix backend scheduler >"$T/server-logs.txt" 2>&1
hexec e2e-host-a journalctl --no-pager >"$T/host-journal.txt"
for needle in "$TOKEN" "$KEYLINE"; do
	if grep -qF "$needle" "$T/server-logs.txt"; then die 'secret leaked into server logs'; fi
	if grep -qF "$needle" "$T/host-journal.txt"; then die 'secret leaked into host journal'; fi
done
pass 'no token/key material in server logs or host journal'

# --- phases 5+6: recovery (A) and offline (B), in parallel --------------------------
# Both wait out the same real 420s proof timeout; running them together
# keeps the suite near 25 minutes instead of 35.
log 'phase 5+6/11: recovery-after-timeout (A) and offline-host (B) in parallel'

scenario_recovery() { # N+1 -> N+2 installs, never proves, operator restores .prev
	publish "$V_BRICK"
	J2=$(make_job "$A_ID" "$V_BRICK")
	wait_until 'recovery claim' 240 probe_job_status "$J2" running
	# Freeze timers the instant the claim lands: otherwise a poll firing
	# between rename and the identity break below would prove N+2 and the
	# timeout we are demonstrating would never happen. The running oneshot
	# is unaffected by stopping its timer.
	hexec e2e-host-a systemctl stop cadence-agent-poll.timer cadence-agent.timer
	wait_until 'recovery rename' 300 probe_canonical_version e2e-host-a "$V_BRICK"
	[ "$(hexec e2e-host-a sha256sum /usr/bin/cadence-agent.prev | awk '{print $1}')" = \
		"$(cat "$T/hash-n1.txt")" ] || die 'recovery: .prev is not N+1 (rotation wrong)'
	pass 'recovery: N+2 installed, .prev rotated to N+1 (not N)'
	# Bad-release simulation, harness-as-adversary: N+2 can no longer
	# authenticate, so no valid N+2 contact ever reaches the server.
	hexec e2e-host-a mv /etc/cadence/agent.env /root/agent.env.bak
	hexec e2e-host-a systemctl start cadence-agent-poll.timer cadence-agent.timer
	log 'recovery: identity broken, waiting out the real proof timeout'
	wait_until 'proof timeout (job failed)' 660 probe_job_status "$J2" failed
	[ "$(job_field "$J2" failure_category)" = upgrade_proof_timeout ] \
		|| die "recovery: category $(job_field "$J2" failure_category)"
	[ "$(host_version e2e-host-a)" = "$V_BRICK" ] || die 'recovery: canonical moved by itself'
	pass 'recovery: job failed upgrade_proof_timeout; canonical untouched (no auto-rollback)'
	# Operator recovery, exactly as documented: restore .prev + identity.
	hexec e2e-host-a install -m 0755 /usr/bin/cadence-agent.prev /usr/bin/cadence-agent
	hexec e2e-host-a mv /root/agent.env.bak /etc/cadence/agent.env
	wait_until 'host-a reports N+1 again' 240 probe_agent_version "$A_ID" "$V_NEXT"
	[ "$(job_field "$J2" status)" = failed ] || die 'recovery: job state rolled back (must stay failed)'
	[ "$(host_version e2e-host-a)" = "$V_NEXT" ] || die 'recovery: canonical is not restored N+1'
	[ "$(hexec e2e-host-a /usr/bin/cadence-agent.prev -version)" = "$V_NEXT" ] \
		|| die 'recovery: .prev changed during recovery (must stay N+1)'
	hexec e2e-host-a sh /root/readiness.sh | grep -q 6B-READY || die 'recovery: host not 6B-READY'
	pass 'recovery: operator restored .prev+identity; host healthy on N+1; job stays failed'
}

scenario_offline() { # claim, stall download, kill host, timeout, resume with old binary
	publish "$V_OFF"
	# Deterministic stall: swap the served amd64 bytes for a FIFO nobody
	# writes to. Caddy blocks serving it, so the claiming agent wedges
	# inside its download (within the 300s local deadline) and the kill
	# below provably lands pre-rename. API traffic is unaffected.
	served="$repo/dist/agent/v$V_OFF/cadence-agent-linux-amd64"
	mv "$served" "$T/offline-amd64.real"
	mkfifo "$served" || die 'offline: cannot stage FIFO stall'
	J3=$(make_job "$B_ID" "$V_OFF")
	wait_until 'offline claim' 240 probe_job_status "$J3" running
	sleep 10 # let the poll reach the wedged fetch
	[ "$(host_version e2e-host-b)" = "$V_N" ] || die 'offline: renamed before the stall (harness bug)'
	docker stop -t 30 e2e-host-b >/dev/null || die 'offline: cannot stop host'
	# Release caddy's reader blocked on the FIFO, then restore real bytes.
	timeout 10 dd if=/dev/null of="$served" >/dev/null 2>&1 || true
	rm -f "$served"
	mv "$T/offline-amd64.real" "$served" || die 'offline: cannot restore served bytes'
	log 'offline: host down mid-download, waiting out the real proof timeout'
	wait_until 'offline proof timeout (job failed)' 660 probe_job_status "$J3" failed
	[ "$(job_field "$J3" failure_category)" = upgrade_proof_timeout ] \
		|| die "offline: category $(job_field "$J3" failure_category)"
	docker start e2e-host-b >/dev/null || die 'offline: cannot restart host'
	wait_until 'host-b systemd back' 180 probe_systemd e2e-host-b
	wait_until 'host-b polls again' 240 probe_agent_version "$B_ID" "$V_N"
	[ "$(job_field "$J3" status)" = failed ] || die 'offline: job state changed after resume'
	[ "$(host_version e2e-host-b)" = "$V_N" ] || die 'offline: LATE RENAME after resume'
	pass 'offline: resumed with old binary, no late rename, job stays failed'
}

(trap - EXIT HUP INT TERM; scenario_recovery >"$T/recovery.log" 2>&1) &
PID_R=$!
(trap - EXIT HUP INT TERM; scenario_offline >"$T/offline.log" 2>&1) &
PID_O=$!
wait $PID_R || die 'recovery scenario failed (see recovery.log)'
wait $PID_O || die 'offline scenario failed (see offline.log)'
pass 'recovery + offline scenarios green (parallel real timeouts)'

# --- phase 7: pre-commit failure (tampered bytes on the server) ----------------------
log 'phase 7/11: pre-commit failure with tampered published bytes'
publish "$V_TAMP"
# Adversary step: flip bytes in the SERVED file after a valid publish.
printf 'TAMPERED-BY-E2E-HARNESS-0000000000000000000000000000000000' |
	dd of="$repo/dist/agent/v$V_TAMP/cadence-agent-linux-amd64" conv=notrunc bs=1 seek=100 \
		>/dev/null 2>&1 || die 'tamper write failed'
J4=$(make_job "$A_ID" "$V_TAMP")
# The refusal lands <1s after the claim, faster than the 5s status polls:
# waiting for `running` would miss it. Wait for the terminal state and
# prove the claim happened from the agent's own journal.
wait_until 'agent refuses tampered bytes' 240 probe_job_status "$J4" failed
hexec e2e-host-a journalctl -u cadence-agent-poll.service --no-pager \
	| grep -q "job received.*$J4" || die 'tampered: no claim in agent journal'
[ "$(job_field "$J4" failure_category)" = upgrade_verification_failed ] \
	|| die "tampered: category $(job_field "$J4" failure_category)"
job_field "$J4" failure_summary | grep -qi 'checksum' \
	|| die "tampered: summary $(job_field "$J4" failure_summary)"
[ "$(host_version e2e-host-a)" = "$V_NEXT" ] || die 'tampered: canonical moved'
[ "$(hexec e2e-host-a sha256sum /usr/bin/cadence-agent.prev | awk '{print $1}')" = \
	"$(cat "$T/hash-n1.txt")" ] || die 'tampered: .prev touched'
snapshot_creds e2e-host-a "$T/creds-a-final.txt"
cmp -s "$T/creds-a-before.txt" "$T/creds-a-final.txt" || die 'tampered: credentials changed'
sleep 30
n_ok=$(psql_e2e "SELECT count(*) FROM webhook_deliveries WHERE event_type = 'job.succeeded' AND payload->'data'->>'job_id' = '$J4'")
[ "$n_ok" = 0 ] || die "tampered: $n_ok succeeded events for a refused job"
pass 'pre-commit failure: agent refused (checksum), canonical/.prev/creds intact, no proof'

# --- campaign helpers -------------------------------------------------------------
campaign_field() { psql_e2e "SELECT $2 FROM campaigns WHERE id = '$1'"; } # CID COL
probe_campaign_status() { [ "$(campaign_field "$1" status)" = "$2" ]; } # CID WANT
campaign_jobs() { psql_e2e "SELECT count(*) FROM jobs WHERE campaign_id = '$1'"; }
probe_campaign_jobs() { [ "$(campaign_jobs "$1")" = "$2" ]; } # CID N
host_agent_jobs() { psql_e2e "SELECT count(*) FROM jobs WHERE host_id = '$1' AND job_type = 'agent_upgrade'"; }
stage_host() { psql_e2e "SELECT host_id FROM campaign_hosts WHERE campaign_id = '$1' AND stage_index = $2"; }
job_epoch() { psql_e2e "SELECT EXTRACT(EPOCH FROM $2)::bigint FROM jobs WHERE id = '$1'"; }
probe_ch_state() { [ "$(psql_e2e "SELECT state FROM campaign_hosts WHERE campaign_id = '$1' AND host_id = '$2'")" = "$3" ]; }
canon_hash() { hexec "$1" sha256sum /usr/bin/cadence-agent | awk '{print $1}'; }
prev_hash() { hexec "$1" sh -c 'sha256sum /usr/bin/cadence-agent.prev 2>/dev/null || echo none' | awk '{print $1}'; }

ctr_for() { # HOSTID -> container name
	if [ "$1" = "$A_ID" ]; then printf e2e-host-a; else printf e2e-host-b; fi
}

make_campaign() { # NAME H1 H2 TARGET STAGES CONC FAIL WINDOW -> id; asserts audit+contract
	resp=$(api POST /api/v1/admin/campaigns \
		"{\"name\":\"$1\",\"job_type\":\"agent_upgrade\",\"job_params\":{\"target_version\":\"$4\"},\"host_ids\":[\"$2\",\"$3\"],\"stages\":$5,\"max_concurrency\":$6,\"max_failures\":$7,\"observation_window_seconds\":$8}")
	cid=$(printf '%s' "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
	[ "$(campaign_field "$cid" job_type)" = agent_upgrade ] || die "campaign $cid wrong type"
	[ "$(psql_e2e "SELECT job_params->>'target_version' FROM campaigns WHERE id = '$cid'")" = "$4" ] \
		|| die "campaign $cid bad target"
	n=$(psql_e2e "SELECT count(*) FROM audit_log WHERE action = 'campaign.create' AND target_id = '$cid'")
	[ "$n" = 1 ] || die "campaign $cid create audit rows: $n"
	printf '%s' "$cid"
}

activate_campaign() { # CID -> asserts running + audit
	api POST "/api/v1/admin/campaigns/$1/activate" >/dev/null
	[ "$(campaign_field "$1" status)" = running ] || die "campaign $1 not running"
	n=$(psql_e2e "SELECT count(*) FROM audit_log WHERE action = 'campaign.activate' AND target_id = '$1'")
	[ "$n" = 1 ] || die "campaign $1 activate audit rows: $n"
}

# --- phase 8: fleet campaign happy path (canary, gate, rest) --------------------------
log 'phase 8/11: fleet campaign (canary, gate, rest)'
publish "$V_CAMP"
[ "$(host_version e2e-host-a)" = "$V_NEXT" ] || die 'fleet: host-a not on N+1'
[ "$(host_version e2e-host-b)" = "$V_N" ] || die 'fleet: host-b not on N'
snapshot_creds e2e-host-a "$T/creds-a-camp-before.txt"
snapshot_creds e2e-host-b "$T/creds-b-camp-before.txt"
HA_PRE=$(canon_hash e2e-host-a); HB_PRE=$(canon_hash e2e-host-b)
BOOTA=$(hexec e2e-host-a cat /proc/sys/kernel/random/boot_id)
BOOTB=$(hexec e2e-host-b cat /proc/sys/kernel/random/boot_id)
BASE_A=$(host_agent_jobs "$A_ID"); BASE_B=$(host_agent_jobs "$B_ID")

# Window 90s, deliberately longer than the 60s engine tick: the reconcile tick
# after a proof always lands inside the window, so both gate checks below
# are deterministic. A 25s window would race the tick and flake.
C1=$(make_campaign fleet-90-0-6 "$A_ID" "$B_ID" "$V_CAMP" '[1, "rest"]' 1 0 90)
CANARY=$(stage_host "$C1" 0)
if [ "$CANARY" = "$A_ID" ]; then OTHER=$B_ID; BASE_OTHER=$BASE_B; else OTHER=$A_ID; BASE_OTHER=$BASE_A; fi
CCTR=$(ctr_for "$CANARY"); OCTR=$(ctr_for "$OTHER")
[ "$(psql_e2e "SELECT count(*) FROM campaign_hosts WHERE campaign_id = '$C1' AND stage_index = 0")" = 1 ] \
	|| die 'fleet: canary stage is not exactly one host'
pass "fleet campaign $C1 created (canary stage = 1 host, target $V_CAMP)"
activate_campaign "$C1"
wait_until 'canary job created' 180 probe_campaign_jobs "$C1" 1
CJ1=$(psql_e2e "SELECT id FROM jobs WHERE campaign_id = '$C1'")
[ "$(job_field "$CJ1" host_id)" = "$CANARY" ] || die 'fleet: canary job on wrong host'
[ "$(host_agent_jobs "$OTHER")" = "$BASE_OTHER" ] || die 'fleet: stage-2 host got a job early'
pass "canary job $CJ1 created for one host only"
wait_until 'canary claim' 240 probe_job_status "$CJ1" running
[ "$(host_agent_jobs "$OTHER")" = "$BASE_OTHER" ] \
	|| die 'fleet: max_concurrency violated (job B while A running)'
pass 'max_concurrency=1 held while canary running'
wait_until 'canary rename' 300 probe_canonical_version "$CCTR" "$V_CAMP"
wait_until 'canary proof' 240 probe_job_status "$CJ1" succeeded
T1DONE=$(job_epoch "$CJ1" completed_at)
wait_until 'canary row done' 180 probe_ch_state "$C1" "$CANARY" "done"
pass 'canary proved; campaign_host done'
sleep 10 # still inside the 90s window (reconcile tick <=65s after proof)
[ "$(host_agent_jobs "$OTHER")" = "$BASE_OTHER" ] \
	|| die 'fleet: stage-2 job created inside the observation window'
pass 'observation gate held (no stage-2 job inside the window)'
wait_until 'stage-2 job' 240 probe_campaign_jobs "$C1" 2
CJ2=$(psql_e2e "SELECT id FROM jobs WHERE campaign_id = '$C1' AND id <> '$CJ1'")
[ "$(job_epoch "$CJ2" created_at)" -ge $((T1DONE + 90)) ] || die 'fleet: gate bypassed (DB timestamps)'
[ "$(job_field "$CJ2" host_id)" = "$OTHER" ] || die 'fleet: stage-2 job on wrong host'
pass 'stage-2 job created after the window (DB timestamps prove the gate)'
wait_until 'stage-2 claim' 240 probe_job_status "$CJ2" running
wait_until 'stage-2 rename' 300 probe_canonical_version "$OCTR" "$V_CAMP"
wait_until 'stage-2 proof' 240 probe_job_status "$CJ2" succeeded
wait_until 'campaign completed' 240 probe_campaign_status "$C1" completed
pass 'fleet campaign completed (canary + rest proved)'
[ "$(host_version e2e-host-a)" = "$V_CAMP" ] || die 'fleet: host-a not on target'
[ "$(host_version e2e-host-b)" = "$V_CAMP" ] || die 'fleet: host-b not on target'
[ "$(prev_hash e2e-host-a)" = "$HA_PRE" ] || die 'fleet: host-a .prev wrong'
[ "$(prev_hash e2e-host-b)" = "$HB_PRE" ] || die 'fleet: host-b .prev wrong'
pass 'both hosts on N+1-target, .prev = pre-campaign canonical each'
[ "$(hexec e2e-host-a cat /proc/sys/kernel/random/boot_id)" = "$BOOTA" ] || die 'fleet: host-a rebooted'
[ "$(hexec e2e-host-b cat /proc/sys/kernel/random/boot_id)" = "$BOOTB" ] || die 'fleet: host-b rebooted'
[ "$(psql_e2e "SELECT count(*) FROM campaign_hosts WHERE campaign_id = '$C1' AND awaited_boot IS NOT NULL")" = 0 ] \
	|| die 'fleet: reboot await leaked into an agent campaign'
pass 'no reboot, no awaited_boot, no 6A machinery touched'
snapshot_creds e2e-host-a "$T/creds-a-camp-after.txt"
snapshot_creds e2e-host-b "$T/creds-b-camp-after.txt"
cmp -s "$T/creds-a-camp-before.txt" "$T/creds-a-camp-after.txt" || die 'fleet: host-a creds changed'
cmp -s "$T/creds-b-camp-before.txt" "$T/creds-b-camp-after.txt" || die 'fleet: host-b creds changed'
pass 'credentials/config identical on both hosts'
for j in "$CJ1" "$CJ2"; do
	[ "$(psql_e2e "SELECT count(*) FROM webhook_deliveries WHERE event_type = 'job.succeeded' AND payload->'data'->>'job_id' = '$j'")" = 1 ] \
		|| die "fleet: job $j event count wrong"
	[ "$(psql_e2e "SELECT count(*) FROM webhook_deliveries WHERE event_type = 'job.failed' AND payload->'data'->>'job_id' = '$j'")" = 0 ] \
		|| die "fleet: job $j has a failed event"
done
[ "$(psql_e2e "SELECT count(*) FROM webhook_deliveries WHERE event_type = 'campaign.completed' AND payload->'data'->>'campaign_id' = '$C1'")" = 1 ] \
	|| die 'fleet: campaign.completed event missing'
pass 'events: one succeeded per job, zero failed, campaign.completed present'

# --- phase 9: blast radius (systemic canary failure halts the fleet) -------------------
log 'phase 9/11: blast radius (systemic canary failure halts the fleet)'
publish "$V_BLAST"
printf 'TAMPERED-BY-E2E-HARNESS-0000000000000000000000000000000000' |
	dd of="$repo/dist/agent/v$V_BLAST/cadence-agent-linux-amd64" conv=notrunc bs=1 seek=100 \
		>/dev/null 2>&1 || die 'blast: tamper write failed'
HA6=$(canon_hash e2e-host-a); HB6=$(canon_hash e2e-host-b)
PA6=$(prev_hash e2e-host-a); PB6=$(prev_hash e2e-host-b)
BASE_A2=$(host_agent_jobs "$A_ID"); BASE_B2=$(host_agent_jobs "$B_ID")
C2=$(make_campaign blast-90-0-7 "$A_ID" "$B_ID" "$V_BLAST" '[1, "rest"]' 1 0 25)
BCAN=$(stage_host "$C2" 0)
if [ "$BCAN" = "$A_ID" ]; then
	BOTHER=$B_ID; BASE_BO=$BASE_B2
	BO_CANON=$HB6; BO_PREV=$PB6; BC_CANON=$HA6; BC_PREV=$PA6
else
	BOTHER=$A_ID; BASE_BO=$BASE_A2
	BO_CANON=$HA6; BO_PREV=$PA6; BC_CANON=$HB6; BC_PREV=$PB6
fi
BCTR=$(ctr_for "$BCAN"); BOCTR=$(ctr_for "$BOTHER")
activate_campaign "$C2"
wait_until 'blast canary job' 180 probe_campaign_jobs "$C2" 1
BJ=$(psql_e2e "SELECT id FROM jobs WHERE campaign_id = '$C2'")
wait_until 'canary refused' 300 probe_job_status "$BJ" failed
[ "$(job_field "$BJ" failure_category)" = upgrade_verification_failed ] \
	|| die "blast: category $(job_field "$BJ" failure_category)"
wait_until 'campaign halted' 180 probe_campaign_status "$C2" stopped
campaign_field "$C2" halt_reason | grep -q upgrade_verification_failed \
	|| die "blast: halt_reason $(campaign_field "$C2" halt_reason)"
pass 'canary failed systemic; campaign halted'
[ "$(host_agent_jobs "$BOTHER")" = "$BASE_BO" ] || die 'blast: stage-2 host received a job'
[ "$(psql_e2e "SELECT count(*) FROM jobs WHERE campaign_id = '$C2' AND host_id = '$BOTHER'")" = 0 ] \
	|| die 'blast: stage-2 job row exists'
[ "$(canon_hash "$BOCTR")" = "$BO_CANON" ] || die 'blast: stage-2 canonical moved'
[ "$(prev_hash "$BOCTR")" = "$BO_PREV" ] || die 'blast: stage-2 .prev touched'
[ "$(canon_hash "$BCTR")" = "$BC_CANON" ] || die 'blast: canary canonical moved (must refuse pre-commit)'
[ "$(prev_hash "$BCTR")" = "$BC_PREV" ] || die 'blast: canary .prev touched'
pass 'blast radius contained: stage 2 never exposed, no rename anywhere'
[ "$(psql_e2e "SELECT count(*) FROM webhook_deliveries WHERE event_type = 'job.failed' AND payload->'data'->>'job_id' = '$BJ'")" = 1 ] \
	|| die 'blast: canary failed event missing'
[ "$(psql_e2e "SELECT count(*) FROM webhook_deliveries WHERE event_type = 'job.succeeded' AND payload->'data'->>'job_id' = '$BJ'")" = 0 ] \
	|| die 'blast: phantom succeeded event'
[ "$(psql_e2e "SELECT count(*) FROM webhook_deliveries WHERE event_type = 'campaign.stopped' AND payload->'data'->>'campaign_id' = '$C2'")" = 1 ] \
	|| die 'blast: campaign.stopped event missing'
pass 'events: one canary failed, campaign.stopped, nothing for stage 2'

# --- phase 10: isolation re-verified + verdict -----------------------------------------
log 'phase 10/11: done'
if [ "$DEV_BACKEND" = none ]; then
	pass 'no dev stack pre-existed; e2e project only'
else
	[ "$(docker inspect --format '{{.Id}}{{.State.StartedAt}}' cadence-backend-1 2>/dev/null)" = "$DEV_BACKEND" ] \
		|| die 'dev stack was recreated mid-run (isolation broken)'
	pass 'dev stack untouched (same container id + start time)'
fi
log "PASS count (main flow): $PASS"
log 'PRIMITIVE UNITAIRE VALIDEE POUR ORCHESTRATION'
log 'FLEET CAMPAIGN E2E GREEN (canary + gate + blast radius contained)'
