#!/bin/sh
# Write a password-protected copy of an agent signing key, after checking it
# really is one of the two keys the agent trusts.
#
# Which keys exist: the ACTIVE key signs releases in CI and lives only as the
# MINISIGN_SECRET_KEY secret of the GitHub environment "release"; it is not
# backed up here. The BACKUP key (agent/minisign-backup.pub) is generated
# offline, never on a Cadence machine, and kept by the maintainer for
# break-glass releases (scripts/build-agent-release.sh). This script and
# restore-signing-key.sh are for that backup key (or, for a rotation, any key
# of the trust set).
#
#   scripts/backup-signing-key.sh [--force] <secret key file>
#
# The secret key file is named explicitly: there is no default location.
# Before anything is written it must sign a scratch file that verifies against
# agent/minisign.pub or agent/minisign-backup.pub (its key ID is printed), so
# the wrong key can never be archived by mistake. The copy is written next to
# it as <secret key file>.enc, encrypted with `minisign -C`: same key
# material, protected by a password minisign asks for itself (twice). The
# password is never an argument, never read or printed by this script; record
# it in your password manager. Server backups (scripts/backup.sh) never
# include key material.
#
# --force replaces an existing <secret key file>.enc. Without it the script
# refuses to overwrite, so a backup made under a known password is never
# silently replaced.
#
# Environment (tests only): CADENCE_PUBLISH_PUBKEY and
# CADENCE_PUBLISH_PUBKEY_BACKUP override the two trust set paths, with a
# warning on stderr.

set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH='' cd -- "$here/.." && pwd)

# shellcheck source=scripts/agent-sign-lib.sh
. "$here/agent-sign-lib.sh"

die() {
	echo "backup-signing-key.sh: $*" >&2
	exit 1
}

usage() {
	echo "usage: backup-signing-key.sh [--force] <secret key file>" >&2
	exit 2
}

force=0
key=""
for a in "$@"; do
	case "$a" in
	--force) force=1 ;;
	-*) usage ;;
	*)
		[ -z "$key" ] || usage
		key=$a
		;;
	esac
done
[ -n "$key" ] || usage

[ -f "$key" ] || die "no secret key file at $key"
enc="$key.enc"
if [ -e "$enc" ] && [ "$force" -ne 1 ]; then
	die "$enc already exists, pass --force to replace it"
fi

trust_set_load "$repo"
trust_set_check || exit 1

tmp=""
scratch=""
cleanup() {
	if [ -n "$tmp" ]; then rm -f -- "$tmp"; fi
	if [ -n "$scratch" ]; then rm -f -- "$scratch" "$scratch.minisig"; fi
	return 0
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

# The key must be one of the trust set: sign a scratch file with it (minisign
# asks the password itself if the key has one) and verify the signature.
scratch=$(mktemp)
echo "agent signing key backup check $$" >"$scratch"
if ! minisign -S -s "$key" -x "$scratch.minisig" -m "$scratch" >/dev/null; then
	die "could not sign with $key (wrong password, or not a minisign secret key)"
fi
if ! who=$(trust_set_verify "$scratch" "$scratch.minisig"); then
	die "$key is not one of the keys the agent trusts: refusing to back it up"
fi
echo "backup-signing-key.sh: $key is the trust set ${who#* } key ${who% *}"

umask 077
tmp="$enc.tmp.$$"
cp -- "$key" "$tmp"
chmod 0600 "$tmp"

echo "backup-signing-key.sh: choose a password for the backup copy."
echo "  It is not stored here or in the repo: record it in your password manager."
minisign -C -s "$tmp"

# Refuse to ship a copy that is not actually encrypted (judged from the key
# file header, not from a password attempt).
if ! key_file_encrypted "$tmp"; then
	die "the copy is not password protected, aborting"
fi

mv -- "$tmp" "$enc"
tmp=""
chmod 0600 "$enc"

echo "backup-signing-key.sh: wrote $enc (password protected)."
echo "  Keep it with your offline secrets. Restore it with scripts/restore-signing-key.sh."
