#!/bin/sh
# Verify a backup produced by scripts/backup.sh can actually be restored.
#
# Restores the database dump and the Caddy data volume into a THROWAWAY set of
# containers/volumes (all named cadence-rt-* / cadence_rt_*), checks them, and
# tears everything down. It never touches the live stack.
#
#   scripts/restore-check.sh [BACKUP_DIR]
#
# BACKUP_DIR defaults to the newest directory under CADENCE_BACKUP_DIR
# (default <repo>/backups).
#
# Checks:
#   1. pg_restore loads the dump without error
#   2. alembic current on the restored DB == the revision in MANIFEST
#   3. GET /api/v1/hosts on a backend bound to the restored DB returns the
#      same hostnames as the live database
#   4. the restored Caddy CA + issued cert form a valid chain, and a Caddy
#      started on the restored volume serves TLS that verifies against it

set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH='' cd -- "$here/.." && pwd)
cd "$repo"

backup_dir=${CADENCE_BACKUP_DIR:-$repo/backups}
# backup dirs are timestamp-named (no spaces/newlines), so ls is safe here
# shellcheck disable=SC2012
src=${1:-$(ls -1d "$backup_dir"/*/ 2>/dev/null | sort | tail -n 1)}
src=${src%/}
if [ -z "$src" ] || [ ! -f "$src/db.dump" ]; then
	echo "restore-check.sh: no usable backup at '${src:-<none>}'" >&2
	exit 1
fi
echo "restore-check.sh: source = $src"

pg_user=$(sed -n 's/^POSTGRES_USER=//p' "$src/env" | head -n 1); pg_user=${pg_user:-cadence}
pg_pass=$(sed -n 's/^POSTGRES_PASSWORD=//p' "$src/env" | head -n 1)
pg_db=$(sed -n 's/^POSTGRES_DB=//p' "$src/env" | head -n 1); pg_db=${pg_db:-cadence}
want_rev=$(sed -n 's/^alembic_rev  *//p' "$src/MANIFEST" | head -n 1)

net=cadence-rt-net
db=cadence-rt-db
be=cadence-rt-backend
cad=cadence-rt-caddy
pgvol=cadence_rt_pgdata
cadvol=cadence_rt_caddy
fail=0

cleanup() {
	docker rm -f "$db" "$be" "$cad" >/dev/null 2>&1 || true
	docker network rm "$net" >/dev/null 2>&1 || true
	docker volume rm -f "$pgvol" "$cadvol" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

docker network create "$net" >/dev/null
docker volume create "$pgvol" >/dev/null
docker volume create "$cadvol" >/dev/null

# --- restore the Caddy data volume from the tarball ------------------------
docker run --rm -v "$cadvol":/v -v "$src":/b:ro postgres:16 \
	sh -c 'tar xzf /b/caddy_data.tgz -C /v' >/dev/null

# --- bring up a fresh Postgres and restore the dump ----------------------
docker run -d --name "$db" --network "$net" \
	-e POSTGRES_USER="$pg_user" -e POSTGRES_PASSWORD="$pg_pass" \
	-e POSTGRES_DB="$pg_db" -v "$pgvol":/var/lib/postgresql/data \
	postgres:16 >/dev/null

printf 'restore-check.sh: waiting for postgres'
# The image bootstraps a new cluster then restarts the server, so a single
# pg_isready can catch the transient bootstrap postmaster. Require an actual
# query to succeed, twice in a row.
ok=0
i=0
while [ "$ok" -lt 2 ]; do
	i=$((i + 1)); [ "$i" -gt 90 ] && { echo " timeout" >&2; exit 1; }
	if docker exec "$db" psql -U "$pg_user" -d "$pg_db" -c 'SELECT 1' >/dev/null 2>&1; then
		ok=$((ok + 1))
	else
		ok=0
	fi
	printf '.'; sleep 1
done
echo

# the middle branch is a bare echo, so this really is A-then-else-C here
# shellcheck disable=SC2015
docker exec -i "$db" pg_restore -U "$pg_user" -d "$pg_db" --clean --if-exists \
	<"$src/db.dump" >/tmp/rt_pg_restore.log 2>&1 \
	&& echo "  [ok] 1/4  pg_restore loaded the dump" \
	|| { echo "  [FAIL] 1/4  pg_restore errored:"; cat /tmp/rt_pg_restore.log; fail=1; }

url="postgresql+psycopg2://$pg_user:$pg_pass@$db:5432/$pg_db"

# --- check 2: alembic revision -----------------------------------------
have_rev=$(docker run --rm --network "$net" \
	-e CADENCE_DATABASE_URL="$url" -e CADENCE_ADMIN_KEY=restore-check \
	cadence-backend alembic current 2>/dev/null | awk '/^[0-9]/ {print $1}' | tail -n 1)
if [ -n "$want_rev" ] && [ "$have_rev" = "$want_rev" ]; then
	echo "  [ok] 2/4  alembic current = $have_rev (matches MANIFEST)"
else
	echo "  [FAIL] 2/4  alembic current = '${have_rev:-<none>}', expected '$want_rev'"; fail=1
fi

# --- check 3: GET /hosts matches the live DB --------------------------
docker run -d --name "$be" --network "$net" \
	-e CADENCE_DATABASE_URL="$url" -e CADENCE_ADMIN_KEY=restore-check \
	-p 127.0.0.1:18000:8000 cadence-backend >/dev/null
i=0
until curl -sf http://127.0.0.1:18000/healthz >/dev/null 2>&1; do
	i=$((i + 1)); [ "$i" -gt 30 ] && break; sleep 1
done
restored_hosts=$(curl -s http://127.0.0.1:18000/api/v1/hosts \
	| tr ',' '\n' | sed -n 's/.*"hostname":"\([^"]*\)".*/\1/p' | sort)
live_hosts=$(docker compose exec -T db psql -U "$pg_user" -d "$pg_db" -At \
	-c 'SELECT hostname FROM hosts ORDER BY hostname' 2>/dev/null | sort)
if [ -n "$restored_hosts" ] && [ "$restored_hosts" = "$live_hosts" ]; then
	echo "  [ok] 3/4  GET /hosts matches live ($(echo "$restored_hosts" | wc -l | tr -d ' ') host(s))"
else
	echo "  [FAIL] 3/4  restored hosts differ from live"
	echo "    restored: $(echo "$restored_hosts" | tr '\n' ' ')"
	echo "    live:     $(echo "$live_hosts" | tr '\n' ' ')"; fail=1
fi

# --- check 4: restored CA chain + live TLS handshake -----------------
docker run --rm -v "$cadvol":/v:ro postgres:16 sh -c '
	cd /v/caddy/pki/authorities/local &&
	openssl verify -CAfile root.crt -untrusted intermediate.crt \
		/v/caddy/certificates/local/*/*.crt' >/tmp/rt_openssl.log 2>&1 \
	&& chain_ok=1 || chain_ok=0

docker run -d --name "$cad" --network "$net" \
	-v "$cadvol":/data -v "$repo/Caddyfile":/etc/caddy/Caddyfile:ro \
	-e CADENCE_SITE_ADDRESS=cadence.lan -e CADENCE_DASHBOARD_AUTH=off \
	-p 127.0.0.1:18443:443 \
	caddy:2-alpine >/dev/null
docker run --rm -v "$cadvol":/v:ro postgres:16 \
	cat /v/caddy/pki/authorities/local/root.crt >/tmp/rt_root.crt 2>/dev/null
sleep 3
verify_res=$(curl -s -o /dev/null \
	--cacert /tmp/rt_root.crt --resolve cadence.lan:18443:127.0.0.1 \
	-w '%{ssl_verify_result}' "https://cadence.lan:18443/" 2>/dev/null || true)
if [ "$chain_ok" = 1 ] && [ "$verify_res" = "0" ]; then
	echo "  [ok] 4/4  restored CA verifies its cert; Caddy TLS trusts the restored root"
else
	echo "  [FAIL] 4/4  chain_ok=$chain_ok ssl_verify_result='$verify_res'"
	cat /tmp/rt_openssl.log; fail=1
fi

echo
if [ "$fail" = 0 ]; then
	echo "restore-check.sh: PASS"
else
	echo "restore-check.sh: FAIL"
fi
exit "$fail"
