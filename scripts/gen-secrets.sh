#!/bin/sh
# Create .env from .env.example with freshly generated secrets. Run once, on
# the central server, before the first `docker compose up -d --build`.
#
#   scripts/gen-secrets.sh
#
# Needs `openssl` and Docker (for `caddy hash-password`). Refuses to overwrite
# an existing .env. Prints the dashboard login once -- it is not recoverable
# from .env afterwards (only the bcrypt hash is stored).

set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH='' cd -- "$here/.." && pwd)
cd "$repo"

if [ -e .env ]; then
	echo "gen-secrets.sh: .env already exists -- refusing to overwrite" >&2
	exit 1
fi
for cmd in openssl docker; do
	if ! command -v "$cmd" >/dev/null 2>&1; then
		echo "gen-secrets.sh: $cmd not found on PATH" >&2
		exit 1
	fi
done

pg_pw=$(openssl rand -hex 16)
admin_key=$(openssl rand -hex 32)
# Fernet needs urlsafe-base64 of 32 random bytes, not hex -- same encoding
# Fernet.generate_key() itself produces, without needing Python on the host.
token_enc_key=$(openssl rand -base64 32 | tr '+/' '-_')
proxy_key=$(openssl rand -hex 32)
dash_user=cadence
dash_pw=$(openssl rand -hex 12)
dash_hash=$(docker run --rm caddy:2-alpine caddy hash-password --plaintext "$dash_pw")
# docker compose interpolates single `$` in .env -- store the hash $-doubled.
dash_hash_esc=$(printf '%s' "$dash_hash" | sed 's/[$]/$$/g')

sed \
	-e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$pg_pw|" \
	-e "s|^CADENCE_ADMIN_KEY=.*|CADENCE_ADMIN_KEY=$admin_key|" \
	-e "s|^CADENCE_TOKEN_ENCRYPTION_KEY=.*|CADENCE_TOKEN_ENCRYPTION_KEY=$token_enc_key|" \
	-e "s|^CADENCE_INTERNAL_PROXY_KEY=.*|CADENCE_INTERNAL_PROXY_KEY=$proxy_key|" \
	-e "s|^CADENCE_DASHBOARD_USER=.*|CADENCE_DASHBOARD_USER=$dash_user|" \
	-e "s|^CADENCE_DASHBOARD_PASSWORD_HASH=.*|CADENCE_DASHBOARD_PASSWORD_HASH=$dash_hash_esc|" \
	.env.example >.env
chmod 0600 .env

# The hash in .env must un-double back to exactly what caddy produced, or the
# basic-auth password is silently broken (docker compose eats single `$`).
stored=$(grep '^CADENCE_DASHBOARD_PASSWORD_HASH=' .env | cut -d= -f2- | sed 's/[$][$]/$/g')
if [ "$stored" != "$dash_hash" ]; then
	echo "gen-secrets.sh: failed to escape the dashboard hash into .env" >&2
	rm -f .env
	exit 1
fi

cat <<EOF
gen-secrets.sh: wrote .env (0600).

  Dashboard login (shown once -- only the bcrypt hash is kept in .env):

      user:     $dash_user
      password: $dash_pw

  POSTGRES_PASSWORD, CADENCE_ADMIN_KEY, CADENCE_TOKEN_ENCRYPTION_KEY and
  CADENCE_INTERNAL_PROXY_KEY were generated too. Review CADENCE_SITE_ADDRESS and
  CADENCE_HTTP_BIND (127.0.0.1 by
  default -- set 0.0.0.0 to serve the LAN), then:

      docker compose up -d --build
EOF
