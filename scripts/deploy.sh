#!/bin/sh
# Redeploy the Cadence central server in one step: rebuild and restart the
# stack, let Alembic migrate on boot, and re-stage the agent bootstrap assets
# so the binary Caddy serves always matches this checkout.
#
# Run on the central server, from a checkout of this repo, after `git pull`.
#
#   scripts/deploy.sh
#
# This is the documented redeploy command. `docker compose up -d --build` on
# its own still works for a stack-only change, but it does NOT restage the
# agent -- forgetting that step is how a stale agent binary keeps being served.

set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH='' cd -- "$here/.." && pwd)
cd "$repo"

echo "deploy.sh: rebuilding and starting the stack"
docker compose up -d --build --wait

echo "deploy.sh: migration head"
docker compose run --rm backend alembic current

echo "deploy.sh: staging the agent bootstrap assets"
"$here/publish-agent.sh"

echo "deploy.sh: done"
