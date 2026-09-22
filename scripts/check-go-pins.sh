#!/bin/sh
# Fail when the Go version pins scattered across the repo disagree.
#
# Pinned files (every `golang:X.Y` image, `go-version` and `go.mod` pin must
# carry the same major.minor -- run this in CI and before any Go bump):
#   agent/go.mod, .gitlab-ci.yml, .github/workflows/ci.yml,
#   .github/workflows/release.yml, scripts/publish-agent.sh,
#   README.md, CONTRIBUTING.md, CLAUDE.md
# Files absent from the tree (e.g. the private-only ones on the public
# mirror) are skipped with a notice. A listed file that is present but
# carries no pin is an error -- the list above is stale then.
#
#   sh scripts/check-go-pins.sh

set -eu

cd "$(dirname -- "$0")/.."

files="agent/go.mod
.gitlab-ci.yml
.github/workflows/ci.yml
.github/workflows/release.yml
scripts/publish-agent.sh
README.md
CONTRIBUTING.md
CLAUDE.md"

versions=""
failed=0
for f in $files; do
	if [ ! -f "$f" ]; then
		echo "go-pins: $f absent, skipped"
		continue
	fi
	v=$(grep -E -o -e "golang:[0-9]+\\.[0-9]+" -e "go-version: *[\"']?[0-9]+\\.[0-9]+" -e "^go [0-9]+\\.[0-9]+" "$f" \
		| grep -E -o "[0-9]+\\.[0-9]+" || true)
	if [ -z "$v" ]; then
		echo "go-pins: FAIL: $f lists no Go pin" >&2
		failed=1
		continue
	fi
	for one in $v; do
		echo "go-pins: $f -> $one"
		versions="$versions $one"
	done
done

# shellcheck disable=SC2086
single=$(printf '%s\n' $versions | sort -u)
unique=$(printf '%s\n' "$single" | grep -c .)
if [ "$failed" -ne 0 ]; then
	echo "go-pins: FAIL: one or more files carry no pin" >&2
	exit 1
fi
if [ "$unique" -ne 1 ]; then
	echo "go-pins: FAIL: Go pins disagree (want exactly one major.minor)" >&2
	exit 1
fi
echo "go-pins: OK, all pins at $single"
