#!/bin/sh
# Rotate the dashboard basic-auth password in an existing .env, safely.
#
#   scripts/rotate-dashboard-password.sh
#
# Generates a new password, hashes it with `caddy hash-password`, and writes it
# into .env `$`-doubled -- docker compose interpolates single `$`, so a raw
# bcrypt hash would be corrupted. Backs .env up first and prints the new
# password once. Then apply it:  docker compose up -d caddy
#
# Needs `openssl` and Docker. For the very first setup use gen-secrets.sh.

set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH='' cd -- "$here/.." && pwd)
cd "$repo"

[ -f .env ] || {
	echo "rotate-dashboard-password.sh: no .env in $repo (run gen-secrets.sh first)" >&2
	exit 1
}
for cmd in openssl docker; do
	command -v "$cmd" >/dev/null 2>&1 || {
		echo "rotate-dashboard-password.sh: $cmd not found on PATH" >&2
		exit 1
	}
done
grep -q '^CADENCE_DASHBOARD_PASSWORD_HASH=' .env || {
	echo "rotate-dashboard-password.sh: .env has no CADENCE_DASHBOARD_PASSWORD_HASH line" >&2
	exit 1
}

new_pw=$(openssl rand -hex 12)
new_hash=$(docker run --rm caddy:2-alpine caddy hash-password --plaintext "$new_pw")
# double every `$` so compose's interpolation leaves the hash intact
new_hash_esc=$(printf '%s' "$new_hash" | sed 's/[$]/$$/g')

backup=".env.bak.$(date +%Y%m%d%H%M%S)"
cp .env "$backup"

tmp=$(mktemp)
sed "s|^CADENCE_DASHBOARD_PASSWORD_HASH=.*|CADENCE_DASHBOARD_PASSWORD_HASH=$new_hash_esc|" .env >"$tmp"
cat "$tmp" >.env
rm -f "$tmp"
chmod 0600 .env

stored=$(grep '^CADENCE_DASHBOARD_PASSWORD_HASH=' .env | cut -d= -f2- | sed 's/[$][$]/$/g')
if [ "$stored" != "$new_hash" ]; then
	echo "rotate-dashboard-password.sh: escaping failed, restoring $backup" >&2
	cp "$backup" .env
	exit 1
fi

dash_user=$(grep '^CADENCE_DASHBOARD_USER=' .env | cut -d= -f2-)
cat <<EOF
rotate-dashboard-password.sh: .env updated (previous copy: $backup).

  New dashboard login (shown once):

      user:     ${dash_user:-cadence}
      password: $new_pw

  Apply it:  docker compose up -d caddy
EOF
