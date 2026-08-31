#!/bin/sh
# Create .env from .env.example with freshly generated secrets. Run once, on
# the central server, before the first `docker compose up -d --build`.
#
#   scripts/gen-secrets.sh
#
# Needs `openssl` on PATH. Refuses to overwrite an existing .env.

set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/.." && pwd)
cd "$repo"

if [ -e .env ]; then
	echo "gen-secrets.sh: .env already exists -- refusing to overwrite" >&2
	exit 1
fi
if ! command -v openssl >/dev/null 2>&1; then
	echo "gen-secrets.sh: openssl not found on PATH" >&2
	exit 1
fi

pw=$(openssl rand -hex 16)
admin=$(openssl rand -hex 32)

sed -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$pw|" \
	-e "s|^CADENCE_ADMIN_KEY=.*|CADENCE_ADMIN_KEY=$admin|" \
	.env.example >.env
chmod 0600 .env

echo "gen-secrets.sh: wrote .env (0600) with a generated POSTGRES_PASSWORD and CADENCE_ADMIN_KEY."
echo "  Review CADENCE_SITE_ADDRESS and the *_BIND settings, then: docker compose up -d --build"
