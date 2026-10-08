#!/bin/sh
# Fail when the agent binary install path diverges from the canonical one.
#
# /usr/bin/cadence-agent is the single canonical path for every install
# channel (scripts/agent-install.sh, agent/systemd/install.sh, .deb). The
# repo units carry it directly, so packaging must not rewrite the binary
# path (only the /usr/local/share/doc -> /usr/share/doc rewrite remains
# for the .deb). Run this in CI (both CIs, `scripts` job):
#
#   sh scripts/check-agent-paths.sh
#
# Unlike check-go-pins.sh, every file listed here exists on both mirrors,
# so a missing file is an error, never a skip.

set -eu

cd "$(dirname -- "$0")/.."

failed=0
fail() {
	echo "agent-paths: FAIL: $1" >&2
	failed=1
}

# 1. Repo units: exact ExecStart per service, no old path anywhere.
check_unit() {
	unit=$1
	want=$2
	if [ ! -f "agent/systemd/$unit" ]; then
		fail "agent/systemd/$unit is missing"
		return
	fi
	got=$(grep -c '^ExecStart=' "agent/systemd/$unit" || true)
	if [ "$got" != "1" ]; then
		fail "agent/systemd/$unit has $got ExecStart lines, want exactly 1"
		return
	fi
	line=$(grep '^ExecStart=' "agent/systemd/$unit")
	if [ "$line" != "ExecStart=$want" ]; then
		fail "agent/systemd/$unit: got '$line', want 'ExecStart=$want'"
		return
	fi
	echo "agent-paths: agent/systemd/$unit -> $want"
}

check_unit cadence-agent.service "/usr/bin/cadence-agent"
check_unit cadence-agent-poll.service "/usr/bin/cadence-agent -poll"
check_unit cadence-agent-health-check-boot.service "/usr/bin/cadence-agent -health-check-boot"

if grep -r 'usr/local/bin/cadence-agent' agent/systemd/*.service >/dev/null 2>&1; then
	fail "old binary path still present in agent/systemd/*.service"
fi

# 2. Install scripts: install and invoke the canonical path.
if [ ! -f scripts/agent-install.sh ]; then
	fail "scripts/agent-install.sh is missing"
else
	block_failed=$failed
	# $temporary is matched literally on purpose (grep -F below).
	# shellcheck disable=SC2016
	for line in 'install -m 0755 "$temporary/cadence-agent" /usr/bin/cadence-agent' \
		'/usr/bin/cadence-agent -enroll -enroll-server' \
		'/usr/bin/cadence-agent -version'; do
		if ! grep -qF "$line" scripts/agent-install.sh; then
			fail "scripts/agent-install.sh lacks '$line'"
		fi
	done
	if grep -q 'usr/local/bin/cadence-agent' scripts/agent-install.sh; then
		fail "old binary path still present in scripts/agent-install.sh"
	fi
	if [ "$failed" -eq "$block_failed" ]; then
		echo "agent-paths: scripts/agent-install.sh installs/invokes /usr/bin/cadence-agent"
	fi
fi

if [ ! -f agent/systemd/install.sh ]; then
	fail "agent/systemd/install.sh is missing"
else
	block_failed=$failed
	if ! grep -q '^PREFIX=/usr/bin$' agent/systemd/install.sh; then
		fail "agent/systemd/install.sh PREFIX is not /usr/bin"
	fi
	if grep -q 'usr/local/bin/cadence-agent' agent/systemd/install.sh; then
		fail "old binary path still present in agent/systemd/install.sh"
	fi
	if [ "$failed" -eq "$block_failed" ]; then
		echo "agent-paths: agent/systemd/install.sh PREFIX=/usr/bin"
	fi
fi

# 3. .deb packaging: binary already canonical, no binary-path rewrite.
if [ ! -f packaging/nfpm.yaml ]; then
	fail "packaging/nfpm.yaml is missing"
else
	if ! grep -q 'dst: /usr/bin/cadence-agent' packaging/nfpm.yaml; then
		fail "packaging/nfpm.yaml does not install to /usr/bin/cadence-agent"
	fi
	if grep -q 'usr/local/bin' packaging/nfpm.yaml; then
		fail "packaging/nfpm.yaml still references /usr/local/bin"
	fi
	echo "agent-paths: packaging/nfpm.yaml dst is /usr/bin/cadence-agent"
fi

if [ ! -f .github/workflows/release.yml ]; then
	fail ".github/workflows/release.yml is missing"
else
	block_failed=$failed
	if grep -q 's#/usr/local/bin#/usr/bin#' .github/workflows/release.yml; then
		fail "release.yml still rewrites the binary path for the .deb"
	fi
	if ! grep -q 's#/usr/local/share/doc#/usr/share/doc#' .github/workflows/release.yml; then
		fail "release.yml lost the doc-path rewrite for the .deb"
	fi
	if [ "$failed" -eq "$block_failed" ]; then
		echo "agent-paths: release.yml rewrites doc paths only"
	fi
fi

# 4. Simulate the .deb unit transform: repo units must already be correct,
# i.e. the remaining rewrite changes no ExecStart line.
block_failed=$failed
for unit in cadence-agent.service cadence-agent-poll.service \
	cadence-agent-health-check-boot.service; do
	if [ ! -f "agent/systemd/$unit" ]; then
		continue # already reported above
	fi
	before=$(grep '^ExecStart=' "agent/systemd/$unit")
	after=$(sed -e 's#/usr/local/share/doc#/usr/share/doc#g' "agent/systemd/$unit" \
		| grep '^ExecStart=')
	if [ "$before" != "$after" ]; then
		fail "packaging transform would change ExecStart in $unit"
	fi
done
if [ "$failed" -eq "$block_failed" ]; then
	echo "agent-paths: packaged units keep canonical ExecStart without binary rewrite"
fi

if [ "$failed" -ne 0 ]; then
	exit 1
fi
echo "agent-paths: OK, all install paths converge on /usr/bin/cadence-agent"
