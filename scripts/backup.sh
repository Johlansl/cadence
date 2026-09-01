#!/bin/sh
# Back up the Cadence central server: the Postgres database and Caddy's data
# volume (its internal CA + issued certs -- lose it and every agent fails TLS
# until re-provisioned).
#
# Run on the central server, from a checkout of this repo, with the stack up.
#
#   scripts/backup.sh
#
# Writes a timestamped directory under CADENCE_BACKUP_DIR containing:
#   db.dump           pg_dump custom format (restore with pg_restore)
#   caddy_data.tgz    the Caddy /data volume (internal CA + certs)
#   env               a copy of .env (secrets: admin key, DB password) -- 0600
#   MANIFEST          timestamp, git commit, alembic revision, sha256 sums
#
# Environment:
#   CADENCE_BACKUP_DIR    base directory for backups (default: <repo>/backups)
#   CADENCE_BACKUP_KEEP   how many timestamped dirs to keep (default: 14)
#   CADENCE_ENV_FILE      .env to read POSTGRES_* from (default: <repo>/.env)

set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH='' cd -- "$here/.." && pwd)
env_file=${CADENCE_ENV_FILE:-$repo/.env}
backup_dir=${CADENCE_BACKUP_DIR:-$repo/backups}
keep=${CADENCE_BACKUP_KEEP:-14}

cd "$repo"

if [ ! -f "$env_file" ]; then
	echo "backup.sh: no env file at $env_file" >&2
	exit 1
fi

pg_user=$(sed -n 's/^POSTGRES_USER=//p' "$env_file" | head -n 1)
pg_db=$(sed -n 's/^POSTGRES_DB=//p' "$env_file" | head -n 1)
pg_user=${pg_user:-cadence}
pg_db=${pg_db:-cadence}

db_cid=$(docker compose ps -q db)
caddy_cid=$(docker compose ps -q caddy)
if [ -z "$db_cid" ] || [ -z "$caddy_cid" ]; then
	echo "backup.sh: the stack must be up (db and caddy containers not found)" >&2
	exit 1
fi

# The real (project-prefixed) name of the volume mounted at Caddy's /data.
caddy_vol=$(docker inspect -f \
	'{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' \
	"$caddy_cid")
if [ -z "$caddy_vol" ]; then
	echo "backup.sh: could not find the caddy /data volume" >&2
	exit 1
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="$backup_dir/$stamp"
mkdir -p "$out"

echo "backup.sh: -> $out"

# 1. Database (custom format; pg_restore handles it with --clean on restore).
docker compose exec -T db pg_dump -U "$pg_user" -d "$pg_db" -Fc >"$out/db.dump"

# 2. Caddy data volume (tar from a throwaway container mounting it read-only;
#    reuse the postgres image so nothing extra is pulled).
docker run --rm -v "$caddy_vol":/v:ro -v "$out":/out postgres:16 \
	tar czf /out/caddy_data.tgz -C /v . >/dev/null

# 3. .env (secrets).
cp "$env_file" "$out/env"
chmod 0600 "$out/env"

# 4. Manifest.
alembic_rev=$(docker compose exec -T backend alembic current 2>/dev/null \
	| awk '/^[0-9]/ {print $1}' | tail -n 1)
{
	echo "created_at   $stamp"
	echo "git_commit   $(git -C "$repo" rev-parse HEAD 2>/dev/null || echo unknown)"
	echo "alembic_rev  ${alembic_rev:-unknown}"
	echo "caddy_volume $caddy_vol"
	echo
	( cd "$out" && sha256sum db.dump caddy_data.tgz env )
} >"$out/MANIFEST"

# 5. Prune old backups, keep the newest $keep.
if [ "$keep" -gt 0 ]; then
	# backup dirs are timestamp-named (no spaces/newlines), so ls is safe here
	# shellcheck disable=SC2012
	ls -1d "$backup_dir"/*/ 2>/dev/null | sort | head -n "-$keep" | while read -r old; do
		echo "backup.sh: pruning $old"
		rm -rf "$old"
	done
fi

echo "backup.sh: done"
ls -l "$out"
