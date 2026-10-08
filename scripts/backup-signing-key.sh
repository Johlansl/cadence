#!/bin/sh
# Write a passphrase-protected backup of the agent signing key.
#
# Run on the KEY-HOLDER machine (operator laptop/vault) -- never on the
# Cadence server. The release key lives only there; the server verifies
# with agent/minisign.pub and must never see the private half.
#
# This makes a copy of the live key encrypted at rest with `minisign -C`:
# same key material, protected by a passphrase you type here and record in
# your password manager. Server backups (scripts/backup.sh) deliberately do
# NOT include it; store this copy with your other offline secrets.
#
# Run it once now, and again whenever you rotate the key
# (docs/decisions.md, "Agent distribution / signing").
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
echo "  Keep it with your offline secrets; server backups never include it."
echo "  Restore with scripts/restore-signing-key.sh."
