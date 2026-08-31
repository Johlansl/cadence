#!/bin/sh
# Image entrypoint for the backend and the scheduler (they share this image).
#
# For the long-running app processes, wait for the database and apply pending
# Alembic migrations first (app.prestart), then hand off to the real command.
# For anything else -- `alembic ...`, a shell for the test run -- do nothing
# and exec straight through.
set -e

case "$1" in
	uvicorn | python)
		python -m app.prestart
		;;
esac

exec "$@"
