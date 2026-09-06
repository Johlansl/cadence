#!/bin/sh
# Write a passphrase-protected backup of the agent signing key.
#
# The live key (scripts/publish-agent.sh reads it) MUST stay passwordless, so it
# is never in scripts/backup.sh's output. This makes a separate copy encrypted
# at rest with `minisign -C`: same key material, protected by a passphrase you
# type here and record in your password manager. scripts/backup.sh then folds
# that encrypted copy into every nightly backup.
#
# Run on the central server, from a checkout of this repo. Run it once now, and
# again whenever you rotate the key (docs/decisions.md, "Agent distribution /
# signing").
#
#   scripts/backup-signing-key.sh [--force]
#
# Environment:
#   CADENCE_MINISIGN_KEY   live secret key (default: ~/.cadence/minisign.key)
#
# --force replaces an existing ~/.cadence/minisign.key.enc (e.g. after a
# rotation). Without it the script refuses to overwrite, so a backup made under
# a known passphrase is never silently replaced.

set -eu

force=0
[ "${1:-}" = "--force" ] && force=1

key=${CADENCE_MINISIGN_KEY:-$HOME/.cadence/minisign.key}
enc="$key.enc"

if [ ! -f "$key" ]; then
	echo "backup-signing-key.sh: no signing key at $key" >&2
	exit 1
fi
if [ -f "$enc" ] && [ "$force" -ne 1 ]; then
	echo "backup-signing-key.sh: $enc already exists -- pass --force to replace it" >&2
	exit 1
fi

mkdir -p "$(dirname "$enc")"
tmp="$enc.tmp.$$"
chk="$enc.chk.$$"
trap 'rm -f "$tmp" "$chk"' EXIT

cp "$key" "$tmp"
chmod 0600 "$tmp"

echo "backup-signing-key.sh: choose a passphrase for the backup copy."
echo "  It is NOT stored here or in the repo -- record it in your password manager."
minisign -C -s "$tmp"

# Refuse to ship a copy that is not actually encrypted: removing the password
# with an empty one must fail on a protected key.
cp "$tmp" "$chk"
if printf '\n' | minisign -C -W -s "$chk" 2>&1 | grep -q 'Password removed'; then
	echo "backup-signing-key.sh: the copy is not password-protected -- aborting" >&2
	exit 1
fi

mv "$tmp" "$enc"
chmod 0600 "$enc"

echo "backup-signing-key.sh: wrote $enc (password-protected)."
echo "  It is included in every scripts/backup.sh run from now on."
echo "  Restore with scripts/restore-signing-key.sh."
