#!/bin/sh
# cadence-agent .deb -- pre-remove. Stop and disable the timers on removal
# (not on upgrade: $1 is "upgrade <ver>" then, and postinstall re-enables).
set -e

case "${1:-remove}" in
	remove | purge | 0 | "")
		[ -d /run/systemd/system ] || exit 0
		systemctl disable --now cadence-agent.timer cadence-agent-poll.timer \
			>/dev/null 2>&1 || true
		;;
esac

exit 0
