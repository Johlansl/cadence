#!/bin/sh
# Restore the agent signing key from a passphrase-protected backup made by
# scripts/backup-signing-key.sh (and carried in scripts/backup.sh output).
#
# Prompts for the passphrase, writes the passwordless live key back to
# ~/.cadence/minisign.key, and verifies it by re-signing a scratch file and
# checking the signature against the committed agent/minisign.pub. A wrong
# passphrase or a wrong backup fails that check and nothing is installed.
#
#   scripts/restore-signing-key.sh [--force] [SOURCE]
#
# SOURCE is a minisign.key.enc file or a backups/<timestamp> directory
# (default: the newest backup that has one, else ~/.cadence/minisign.key.enc).
# --force overwrites an existing live key.
#
# Environment:
#   CADENCE_MINISIGN_KEY   live secret key to write (default: ~/.cadence/minisign.key)
#   CADENCE_BACKUP_DIR     where backups live (default: <repo>/backups)

set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH='' cd -- "$here/.." && pwd)
cd "$repo"

force=0
src_arg=""
for a in "$@"; do
	case "$a" in
	--force) force=1 ;;
	*) src_arg="$a" ;;
	esac
done

backup_dir=${CADENCE_BACKUP_DIR:-$repo/backups}
target=${CADENCE_MINISIGN_KEY:-$HOME/.cadence/minisign.key}
pub="$repo/agent/minisign.pub"

if [ -n "$src_arg" ]; then
	if [ -d "$src_arg" ]; then src="$src_arg/minisign.key.enc"; else src="$src_arg"; fi
elif [ -f "$HOME/.cadence/minisign.key.enc" ] && [ ! -d "$backup_dir" ]; then
	src="$HOME/.cadence/minisign.key.enc"
else
	# newest backup dir that actually has a minisign.key.enc
	# backup dirs are timestamp-named (no spaces/newlines), so ls is safe here
	# shellcheck disable=SC2012
	src=$(ls -1d "$backup_dir"/*/ 2>/dev/null | sort -r | while read -r d; do
		[ -f "$d/minisign.key.enc" ] && { echo "$d/minisign.key.enc"; break; }
	done)
	[ -n "$src" ] || src="$HOME/.cadence/minisign.key.enc"
fi

if [ ! -f "$src" ]; then
	echo "restore-signing-key.sh: no encrypted key at '${src:-<none>}'" >&2
	exit 1
fi
if [ ! -f "$pub" ]; then
	echo "restore-signing-key.sh: no $pub to verify against" >&2
	exit 1
fi
if [ -f "$target" ] && [ "$force" -ne 1 ]; then
	echo "restore-signing-key.sh: $target already exists -- pass --force to replace it" >&2
	exit 1
fi

echo "restore-signing-key.sh: source = $src"

mkdir -p "$(dirname "$target")"
tmp="$target.tmp.$$"
scratch=$(mktemp)
trap 'rm -f "$tmp" "$scratch" "$scratch.minisig"' EXIT

cp "$src" "$tmp"
chmod 0600 "$tmp"

echo "restore-signing-key.sh: enter the backup passphrase to unlock the key."
minisign -C -W -s "$tmp"

# Verify: the restored key must produce a signature that agent/minisign.pub
# accepts. Fails on a wrong passphrase or a stale/foreign backup.
echo "cadence signing-key restore check $$" >"$scratch"
if ! minisign -S -s "$tmp" -m "$scratch" >/dev/null 2>&1 \
	|| ! minisign -Vm "$scratch" -p "$pub" >/dev/null 2>&1; then
	echo "restore-signing-key.sh: restored key does NOT match $pub -- not installing" >&2
	exit 1
fi

mv "$tmp" "$target"
chmod 0600 "$target"
echo "restore-signing-key.sh: wrote $target (passwordless); verified against agent/minisign.pub."
echo "  scripts/publish-agent.sh / scripts/deploy.sh work again."
