#!/bin/sh
# Redeploy the Cadence central server in one step: rebuild and restart the
# stack, let Alembic migrate on boot, and re-stage the agent bootstrap
# assets (installer, units, CA).
#
# Agent release binaries are NOT published here: a version is built and
# signed on the key-holder machine (scripts/build-agent-release.sh) and
# staged explicitly (scripts/publish-agent.sh publish). Deploying the
# backend/frontend never requires one.
#
# Run on the central server, from a checkout of this repo, after `git pull`.
#
#   scripts/deploy.sh
#
# This is the documented redeploy command. `docker compose up -d --build` on
# its own still works for a stack-only change, but it does NOT restage the
# bootstrap assets and does NOT recreate caddy to pick up a host-edited
# Caddyfile or the freshly staged assets.

set -eu

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH='' cd -- "$here/.." && pwd)
cd "$repo"

echo "deploy.sh: rebuilding and starting the stack"
docker compose up -d --build --wait

echo "deploy.sh: migration head"
docker compose run --rm backend alembic current

echo "deploy.sh: staging the agent bootstrap assets"
"$here/publish-agent.sh" assets

# caddy bind-mounts the Caddyfile, caddy/entrypoint.sh and ./dist. A host-side
# edit (atomic write = new inode) or a recreated dist/ leaves the running
# container on the old content: `caddy reload` then reports the config is
# unchanged, and `up -d --build` does not recreate caddy because its image and
# service spec are unchanged. Force a recreate every deploy so a Caddyfile edit
# and the freshly staged assets always take effect. A few seconds of dashboard
# downtime; agents retry and reports are durable.
echo "deploy.sh: recreating caddy to pick up mounted-file changes"
docker compose up -d --force-recreate --no-deps --wait caddy

echo "deploy.sh: done"
