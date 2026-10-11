#!/bin/sh
# test-ci-parity.sh -- tests for scripts/check-ci-parity.sh.
#
# Runs the REAL script in throwaway trees that hold a copy of the two CI files
# (the committed ones, or a copy with one deliberate difference), and checks
# the exit status and the message. The repository itself is never modified.
#
#   sh scripts/test-ci-parity.sh

set -eu

HERE=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH='' cd -- "$HERE/.." && pwd)

pass=0
failed=0

ok() { printf '  ok   %s\n' "$1"; pass=$((pass + 1)); }
fail() { printf '  FAIL %s\n' "$1"; failed=$((failed + 1)); }

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

GL=$REPO/.gitlab-ci.yml
GH=$REPO/.github/workflows/ci.yml
if [ ! -f "$GL" ] || [ ! -f "$GH" ]; then
	echo "ci-parity-test: needs both CI files in the checkout (the public mirror has no .gitlab-ci.yml): skipped"
	exit 0
fi

GOLANGCI=golangci/golangci-lint:v2.13.2@sha256:ba07dffad130794ae79ebaa0056809d18c0168f3f846480ffd3eb6c04578b83d
ACTIONLINT=rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667
OTHER=sha256:0000000000000000000000000000000000000000000000000000000000000000

# new_tree <name>: a tree with the script and the two committed CI files.
new_tree() {
	TREE=$T/$1
	mkdir -p "$TREE/scripts" "$TREE/.github/workflows"
	cp "$REPO/scripts/check-ci-parity.sh" "$TREE/scripts/"
	cp "$GL" "$TREE/.gitlab-ci.yml"
	cp "$GH" "$TREE/.github/workflows/ci.yml"
}

# run_tree: runs the script of $TREE, sets RC, output in $TREE/out.
run_tree() {
	RC=0
	sh "$TREE/scripts/check-ci-parity.sh" >"$TREE/out" 2>&1 || RC=$?
}

# edit <file> <sed expression>: change one file of the tree, and prove that
# something changed (a mutation that matches nothing would test nothing).
edit() {
	cp "$1" "$1.orig"
	sed -e "$2" "$1.orig" >"$1"
	if cmp -s "$1" "$1.orig"; then
		echo "ci-parity-test: the edit '$2' changed nothing in $1" >&2
		exit 1
	fi
	rm -f "$1.orig"
}

expect_ok() {
	if [ "$RC" -eq 0 ] && grep -qF -- "$2" "$TREE/out"; then
		ok "$1 (exit=$RC)"
	else
		fail "$1 (exit=$RC, want 0 with '$2'): $(cat "$TREE/out")"
	fi
}

expect_drift() {
	if [ "$RC" -eq 1 ] && grep -qF -- "$2" "$TREE/out"; then
		ok "$1 (exit=$RC)"
	else
		fail "$1 (exit=$RC, want 1 with '$2'): $(cat "$TREE/out")"
	fi
}

new_tree committed
run_tree
expect_ok "the committed CI files agree" "OK, the two CI configs pin the same tools"
for want in "$GOLANGCI" "$ACTIONLINT"; do
	if [ "$(grep -c -F -- "-> $want" "$TREE/out")" -eq 2 ]; then
		ok "$want is reported once per file"
	else
		fail "$want not reported twice: $(cat "$TREE/out")"
	fi
done

echo "a different digest in one file"
new_tree gl-golangci
edit "$TREE/.gitlab-ci.yml" "s#golangci/golangci-lint:v2.13.2@sha256:ba07[0-9a-f]*#golangci/golangci-lint:v2.13.2@$OTHER#"
run_tree
expect_drift "golangci-lint digest differs, GitLab side" "golangci/golangci-lint is pinned to 2 different references"

new_tree gh-golangci
edit "$TREE/.github/workflows/ci.yml" "s#golangci/golangci-lint:v2.13.2@sha256:ba07[0-9a-f]*#golangci/golangci-lint:v2.13.2@$OTHER#"
run_tree
expect_drift "golangci-lint digest differs, GitHub side" "golangci/golangci-lint is pinned to 2 different references"

new_tree gh-tag
edit "$TREE/.github/workflows/ci.yml" "s#golangci/golangci-lint:v2.13.2@#golangci/golangci-lint:v2.14.0@#"
run_tree
expect_drift "golangci-lint tag differs" "pinned to 2 different references"

new_tree gl-actionlint
edit "$TREE/.gitlab-ci.yml" "s#rhysd/actionlint@sha256:b193[0-9a-f]*#rhysd/actionlint@$OTHER#"
run_tree
expect_drift "actionlint digest differs, GitLab side" "rhysd/actionlint is pinned to 2 different references"

new_tree gh-actionlint
edit "$TREE/.github/workflows/ci.yml" "s#rhysd/actionlint@sha256:b193[0-9a-f]*#rhysd/actionlint@$OTHER#"
run_tree
expect_drift "actionlint digest differs, GitHub side" "rhysd/actionlint is pinned to 2 different references"

echo "a reference that is not pinned, or that is missing"
new_tree nodigest
edit "$TREE/.github/workflows/ci.yml" "s#golangci/golangci-lint:v2.13.2@sha256:[0-9a-f]*#golangci/golangci-lint:v2.13.2#"
run_tree
expect_drift "golangci-lint without a digest" "is not pinned by digest"

new_tree floating
edit "$TREE/.gitlab-ci.yml" "s#rhysd/actionlint@sha256:[0-9a-f]*#rhysd/actionlint:latest#"
run_tree
expect_drift "actionlint on a floating tag" "is not pinned by digest"

new_tree shortdigest
edit "$TREE/.gitlab-ci.yml" "s#rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667#rhysd/actionlint@sha256:b1934ee5#"
run_tree
expect_drift "actionlint with a truncated digest" "is not pinned by digest"

new_tree gone-gh
edit "$TREE/.github/workflows/ci.yml" "s#rhysd/actionlint#some/other-tool#"
run_tree
expect_drift "actionlint missing from the GitHub file" "has no reference to rhysd/actionlint"

new_tree gone-gl
edit "$TREE/.gitlab-ci.yml" "s#golangci/golangci-lint#some/other-tool#g"
run_tree
expect_drift "golangci-lint missing from the GitLab file" "has no reference to golangci/golangci-lint"

echo "the golangci-lint command line"
new_tree cmd
edit "$TREE/.github/workflows/ci.yml" "s#golangci-lint run ./\\.\\.\\.#golangci-lint run ./cmd/...#"
run_tree
expect_drift "different arguments to golangci-lint run" "the two files run golangci-lint with different arguments"

new_tree nocmd
edit "$TREE/.gitlab-ci.yml" "s#golangci-lint run ./\\.\\.\\.#golangci-lint lint ./...#"
run_tree
expect_drift "no golangci-lint run command" "has no 'golangci-lint run' command"

echo "a CI file that is absent (the public mirror has no .gitlab-ci.yml)"
new_tree nogl
rm -f "$TREE/.gitlab-ci.yml"
run_tree
expect_ok "no .gitlab-ci.yml: skipped, not failed" ".gitlab-ci.yml absent, skipped"

new_tree nogh
rm -f "$TREE/.github/workflows/ci.yml"
run_tree
expect_ok "no ci.yml: skipped, not failed" "ci.yml absent, skipped"

echo "ci-parity-test: $pass passed, $failed failed"
[ "$failed" -eq 0 ]
