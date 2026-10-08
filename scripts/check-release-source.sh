#!/bin/sh
# Refuse to build a release from anything but the exact tagged source.
#
# The key-holder machine authorizes fleet-wide executables, so
# build-agent-release.sh calls this first and aborts unless ALL hold for
# the requested version:
#   - strict N.N.N grammar + matching "## <version>" CHANGELOG heading;
#   - tag agent-v<version> exists locally;
#   - HEAD is exactly that tag's commit (no descendant, no suffix, no
#     lookalike: HEAD == commit(agent-v<version>));
#   - no tracked or staged modification (git diff / git diff --cached);
#   - no untracked, non-ignored file under agent/ (it would compile in).
# Untracked scratch files elsewhere and gitignored build outputs are
# tolerated: they cannot change the built bytes.
#
#   sh scripts/check-release-source.sh <version>
#
# Prints the proven provenance (tag commit, HEAD, tree state) on success.
# Fails closed without git: no repository, no release.
#
# Environment:
#   CADENCE_RELEASE_REPO   checkout to check (default: this repo; the test
#                          suite points it at temp repos)

set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=${CADENCE_RELEASE_REPO:-$(CDPATH='' cd -- "$here/.." && pwd)}
cd "$repo" 2>/dev/null || {
	echo "check-release-source.sh: no repo at $repo" >&2
	exit 1
}

if [ $# -ne 1 ]; then
	echo "usage: check-release-source.sh <version>" >&2
	exit 2
fi
ver=$1

case $ver in
*[!0-9.]*|""|.*|*.|*..*)
	echo "check-release-source.sh: '$ver' is not strict N.N.N" >&2
	exit 1
	;;
esac
_v1=${ver%%.*}; _rest=${ver#*.}
case $_rest in *.*) ;; *)
	echo "check-release-source.sh: '$ver' is not strict N.N.N" >&2
	exit 1
	;;
esac
_v2=${_rest%%.*}; _v3=${_rest#*.}
case $_v3 in *.*)
	echo "check-release-source.sh: '$ver' is not strict N.N.N" >&2
	exit 1
	;;
esac
if [ -z "$_v1" ] || [ -z "$_v2" ] || [ -z "$_v3" ]; then
	echo "check-release-source.sh: '$ver' is not strict N.N.N" >&2
	exit 1
fi
# Canonical decimal only (6B): one release, one spelling ("01.2.3" refuses).
for _v in "$_v1" "$_v2" "$_v3"; do
	case $_v in 0*)
		if [ "$_v" != "0" ]; then
			echo "check-release-source.sh: '$ver' is not canonical N.N.N" >&2
			exit 1
		fi
		;;
	esac
done

if [ ! -f agent/CHANGELOG.md ] || ! grep -q "^## $ver$" agent/CHANGELOG.md; then
	echo "check-release-source.sh: no '## $ver' heading in agent/CHANGELOG.md" >&2
	exit 1
fi

if ! command -v git >/dev/null 2>&1; then
	echo "check-release-source.sh: git is not installed" >&2
	exit 1
fi
if ! git rev-parse --verify --quiet "refs/tags/agent-v$ver" >/dev/null; then
	echo "check-release-source.sh: tag agent-v$ver does not exist here" >&2
	exit 1
fi
head=$(git rev-parse HEAD 2>/dev/null || echo '?')
tagged=$(git rev-parse "agent-v$ver^{commit}" 2>/dev/null || echo '?')
if [ "$head" = "?" ] || [ "$tagged" = "?" ] || [ "$head" != "$tagged" ]; then
	echo "check-release-source.sh: HEAD ($head) is not agent-v$ver ($tagged)" >&2
	exit 1
fi

if ! git diff --quiet 2>/dev/null; then
	echo "check-release-source.sh: tracked worktree modifications present" >&2
	exit 1
fi
if ! git diff --cached --quiet 2>/dev/null; then
	echo "check-release-source.sh: staged modifications present" >&2
	exit 1
fi
if [ -n "$(git ls-files --others --exclude-standard -- agent/ 2>/dev/null)" ]; then
	echo "check-release-source.sh: untracked files under agent/ present" >&2
	exit 1
fi

echo "check-release-source.sh: $ver from agent-v$ver ($tagged), tree clean"
