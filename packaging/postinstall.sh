#!/bin/sh
# cadence-agent .deb -- post-install. Enables the timers but does NOT start
# them: the agent needs /etc/cadence/agent.env (server URL + per-host token)
# first, which this package deliberately does not create.
set -e

if [ -d /run/systemd/system ]; then
	systemctl daemon-reload || true
	systemctl enable cadence-agent.timer cadence-agent-poll.timer >/dev/null 2>&1 || true
fi

if [ ! -f /etc/cadence/agent.env ]; then
	cat <<'EOF'
cadence-agent installed. It is NOT running yet -- the package cannot know your
server URL or the per-host token. To finish:

  1. Trust the Cadence server's internal CA on this host (or point the agent at
     a public-CA / plain-HTTP endpoint).
  2. sudo install -D -m 0600 /usr/share/doc/cadence-agent/agent.env.example \
       /etc/cadence/agent.env
     then set CADENCE_SERVER_URL and CADENCE_TOKEN (from scripts/provision-host.sh
     on the server) in that file.
  3. sudo systemctl start cadence-agent.timer cadence-agent-poll.timer

See https://github.com/Johlansl/cadence for the full install notes.
EOF
elif [ -d /run/systemd/system ]; then
	# Upgrade on an already-configured host: reload and let the timers pick up
	# the new binary/units on their next fire.
	systemctl try-restart cadence-agent.timer cadence-agent-poll.timer >/dev/null 2>&1 || true
fi

exit 0
