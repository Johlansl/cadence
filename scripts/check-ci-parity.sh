#!/bin/sh
# Fail when the two CI configs disagree about the tools they pin by digest.
#
# .gitlab-ci.yml (private) and .github/workflows/ci.yml (public) run the same
# checks, and two of them use a tool that is pinned in both files by hand:
#   golangci/golangci-lint   the `agent-lint` job on GitLab, a step of the
#                            `agent` job on GitHub, and the `golangci-lint run`
#                            arguments they pass
#   rhysd/actionlint         the `actionlint` job on both
# Each reference must carry a digest (<image>[:<tag>]@sha256:<64 hex>), appear
# in both files, and be the same string everywhere. Without this, one file is
# bumped and the other left behind, and the two CIs quietly stop checking the
# same thing.
#
# When either CI file is absent (the public mirror has no .gitlab-ci.yml) the
# check is skipped with a notice, like check-go-pins.sh does. A file that is
# present but holds no reference is an error: this list is stale then.
#
#   sh scripts/check-ci-parity.sh

set -eu

cd "$(dirname -- "$0")/.."

gitlab=.gitlab-ci.yml
github=.github/workflows/ci.yml

for f in "$gitlab" "$github"; do
	if [ ! -f "$f" ]; then
		echo "ci-parity: $f absent, skipped"
		exit 0
	fi
done

failed=0
fail() {
	echo "ci-parity: FAIL: $*" >&2
	failed=1
}

# refs <file> <image>: every reference to the image in the file, one per line
# (the image name followed by everything up to a space, quote or backtick).
refs() {
	grep -E -o "$2[^[:space:]\"'\`]*" "$1" || true
}

# check_image <image>: shape, presence and equality of the references.
check_image() {
	_all=""
	for _f in "$gitlab" "$github"; do
		_found=$(refs "$_f" "$1")
		if [ -z "$_found" ]; then
			fail "$_f has no reference to $1"
			continue
		fi
		for _r in $_found; do
			if ! printf '%s\n' "$_r" | grep -E -q "^$1(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}\$"; then
				fail "$_f: '$_r' is not pinned by digest (want $1[:tag]@sha256:<64 hex>)"
				continue
			fi
			echo "ci-parity: $_f -> $_r"
			_all="$_all $_r"
		done
	done
	# shellcheck disable=SC2086
	_distinct=$(printf '%s\n' $_all | sort -u | grep -c . || true)
	if [ "$_distinct" -gt 1 ]; then
		fail "$1 is pinned to $_distinct different references (want exactly one in both files)"
	fi
}

check_image golangci/golangci-lint
check_image rhysd/actionlint

# The golangci-lint command line: same arguments in both files.
_cmds=""
for _f in "$gitlab" "$github"; do
	_c=$(grep -E -o 'golangci-lint run +[^[:space:]]+' "$_f" | tr -s ' ' ' ' | sort -u || true)
	if [ -z "$_c" ]; then
		fail "$_f has no 'golangci-lint run' command"
		continue
	fi
	echo "ci-parity: $_f -> $_c"
	_cmds="$_cmds
$_c"
done
_n=$(printf '%s\n' "$_cmds" | sort -u | grep -c . || true)
if [ "$_n" -gt 1 ]; then
	fail "the two files run golangci-lint with different arguments"
fi

if [ "$failed" -ne 0 ]; then
	echo "ci-parity: FAIL: the two CI configs have drifted apart" >&2
	exit 1
fi
echo "ci-parity: OK, the two CI configs pin the same tools"
