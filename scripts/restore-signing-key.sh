#!/bin/sh
# Restore a password-protected agent signing key made by
# scripts/backup-signing-key.sh, to a location you choose, for one use.
#
# This is the break-glass path: the restored key is what you give to
# scripts/build-agent-release.sh as CADENCE_SIGNING_KEY. The ACTIVE key is not
# restored with this script (it lives in the GitHub environment "release").
#
#   scripts/restore-signing-key.sh [--force] --to <destination> <backup.key.enc>
#
# - The key stays password protected: this script never decrypts it, and has
#   no option to. minisign asks for the password itself (at the terminal);
#   the script never reads, prints or passes a password.
# - --to is required and there is no default. The destination must not be
#   inside a git repository (any of them: this one, the public mirror, a
#   worktree): a key must not be one `git add` away from a push.
# - It is meant to be temporary. Restore to a tmpfs such as /dev/shm so it
#   never reaches a disk, and delete it after use.
# - The key must be one of the two keys the agent trusts: it has to sign a
#   scratch file that verifies against agent/minisign.pub or
#   agent/minisign-backup.pub (its key ID is printed). Anything else, a wrong
#   password included, is refused and nothing is written.
# - --force overwrites an existing destination file.
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
	echo "restore-signing-key.sh: $*" >&2
	exit 1
}

usage() {
	echo "usage: restore-signing-key.sh [--force] --to <destination> <backup.key.enc>" >&2
	exit 2
}

# inside_git_tree <dir>: succeed when <dir> is, or is under, a git work tree
# or a bare repository. Walks up looking for .git (a directory, or the file a
# worktree or submodule has) and for a bare repository layout, then asks git
# itself when it is installed.
inside_git_tree() {
	_d=$1
	while :; do
		if [ -e "$_d/.git" ]; then return 0; fi
		if [ -f "$_d/HEAD" ] && [ -d "$_d/objects" ]; then return 0; fi
		if [ "$_d" = "/" ]; then break; fi
		_d=$(dirname -- "$_d")
	done
	if command -v git >/dev/null 2>&1 && git -C "$1" rev-parse --git-dir >/dev/null 2>&1; then
		return 0
	fi
	return 1
}

force=0
to=""
src=""
while [ "$#" -gt 0 ]; do
	case "$1" in
	--force) force=1 ;;
	--to)
		[ "$#" -ge 2 ] || usage
		to=$2
		shift
		;;
	-*) usage ;;
	*)
		[ -z "$src" ] || usage
		src=$1
		;;
	esac
	shift
done
[ -n "$to" ] || die "--to <destination> is required (no default location; see the header)"
[ -n "$src" ] || usage

[ -f "$src" ] || die "no encrypted key at '$src'"
key_file_encrypted "$src" || die "$src is not password protected: refusing to copy a plain secret key"

parent=$(CDPATH='' cd -- "$(dirname -- "$to")" 2>/dev/null && pwd -P) ||
	die "the directory of '$to' does not exist"
[ -n "$parent" ] || die "cannot resolve the directory of '$to'"
final=$parent/$(basename -- "$to")
if inside_git_tree "$parent"; then
	die "$final is inside a git repository: refusing to put a secret key there"
fi
if [ -L "$final" ] || [ -d "$final" ]; then
	die "$final is a symbolic link or a directory"
fi
if [ -e "$final" ] && [ "$force" -ne 1 ]; then
	die "$final already exists, pass --force to replace it"
fi

trust_set_load "$repo"
trust_set_check || exit 1

umask 077
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

echo "restore-signing-key.sh: source = $src"
tmp="$parent/.restore-signing-key.$$"
cp -- "$src" "$tmp"
chmod 0600 "$tmp"

# Prove the key is a trust set key: signing makes minisign ask the password.
scratch=$(mktemp)
echo "agent signing key restore check $$" >"$scratch"
echo "restore-signing-key.sh: minisign will now ask the password of the backup."
if ! minisign -S -s "$tmp" -x "$scratch.minisig" -m "$scratch" >/dev/null; then
	die "could not unlock the key (wrong password or damaged backup): nothing written"
fi
if ! who=$(trust_set_verify "$scratch" "$scratch.minisig"); then
	die "this key is not one of the keys the agent trusts: nothing written"
fi

mv -- "$tmp" "$final"
tmp=""
echo "restore-signing-key.sh: wrote $final (still password protected): the trust set ${who#* } key ${who% *}."
echo "  Use it as CADENCE_SIGNING_KEY for scripts/build-agent-release.sh."
echo "  Keep it on a tmpfs such as /dev/shm and delete it after use: rm -f '$final'"
