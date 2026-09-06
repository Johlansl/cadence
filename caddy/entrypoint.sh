#!/bin/sh
# Validate the dashboard basic-auth hash, then start Caddy.
#
# docker compose interpolates every `$` in .env values, so a bcrypt hash
# pasted in raw loses its `$` separators -- Caddy would then start with an
# unusable password and NO error. Fail loudly instead. scripts/gen-secrets.sh
# and scripts/rotate-dashboard-password.sh write the hash `$`-doubled so it
# survives; this checks the result at boot.
set -eu

auth=${CADENCE_DASHBOARD_AUTH:-on}
if [ "$auth" != "off" ]; then
	hash=${CADENCE_DASHBOARD_PASSWORD_HASH:-}
	if [ -z "$hash" ]; then
		echo "caddy: CADENCE_DASHBOARD_AUTH is '$auth' but CADENCE_DASHBOARD_PASSWORD_HASH is empty." >&2
		echo "       Run scripts/gen-secrets.sh (first setup) or scripts/rotate-dashboard-password.sh." >&2
		exit 1
	fi
	# single-quoted on purpose: this is a grep ERE, not a shell expansion
	# shellcheck disable=SC2016
	if ! printf '%s' "$hash" | grep -qE '^\$2[abxy]\$[0-9][0-9]\$[./A-Za-z0-9]{53}$'; then
		echo "caddy: CADENCE_DASHBOARD_PASSWORD_HASH is not a valid bcrypt hash:" >&2
		echo "         $hash" >&2
		echo "       Most likely docker compose ate the '\$' separators -- every" >&2
		echo "       '\$' in the hash must be written '\$\$' in .env. gen-secrets.sh" >&2
		echo "       and rotate-dashboard-password.sh do that for you." >&2
		exit 1
	fi
	# Warn (don't fail) on a weak work factor. The regex above guarantees two
	# digits after the second '$'. Caddy's own hash-password emits cost 14; a
	# lower one means someone hand-set it.
	rest=${hash#\$2?\$}
	cost=${rest%%\$*}
	if [ "$cost" -lt 12 ]; then
		echo "caddy: WARNING dashboard password hash bcrypt cost is $cost (< 12) --" >&2
		echo "       regenerate with scripts/rotate-dashboard-password.sh (Caddy uses 14)." >&2
	fi
fi

exec "$@"
