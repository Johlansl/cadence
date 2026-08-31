"""Container prestart: wait for Postgres, then run `alembic upgrade head`.

Run once by the image entrypoint before the real process (uvicorn or the
scheduler) starts, so a deploy is just `docker compose up -d --build` with no
manual migration step.

The backend and the scheduler share the image and start together, so the
migration is guarded by a Postgres session-level advisory lock: whichever
container gets there first migrates, the other blocks on the lock and then
finds the database already at head.

Tunables (env):
  CADENCE_DB_WAIT_SECONDS   how long to wait for the DB to accept connections
                            before giving up (default 60; 0 = try once)
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from sqlalchemy import create_engine, inspect, pool, text
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.core.logging import configure_logging

log = logging.getLogger("cadence.prestart")

# Arbitrary but fixed: int.from_bytes(b"CDNC") -- shared by every container
# that runs this module so they serialise on the same lock.
_MIGRATION_LOCK_KEY = 1128877123

_BACKEND_DIR = Path(__file__).resolve().parent.parent


def _f(**fields: object) -> dict:
    return {"fields": fields}


def wait_for_db() -> None:
    """Block until the database accepts a connection, or raise after the
    configured timeout."""
    deadline = time.monotonic() + settings.db_wait_seconds
    engine = create_engine(settings.database_url, poolclass=pool.NullPool, future=True)
    attempt = 0
    try:
        while True:
            attempt += 1
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                log.info("database is up", extra=_f(attempts=attempt))
                return
            except OperationalError as exc:
                if time.monotonic() >= deadline:
                    log.error(
                        "database not reachable, giving up",
                        extra=_f(attempts=attempt, waited_s=settings.db_wait_seconds),
                    )
                    raise
                if attempt == 1:
                    first_line = (str(exc).splitlines() or [""])[0]
                    log.info("waiting for database", extra=_f(error=first_line))
                time.sleep(1)
    finally:
        engine.dispose()


def run_migrations() -> None:
    """Apply every pending Alembic revision, holding an advisory lock so the
    peer container (same image) does not migrate concurrently.

    If the database already carries the baseline schema but has no
    alembic_version table (a legacy deploy that bootstrapped from init.sql via
    the postgres image's docker-entrypoint-initdb.d), stamp 0001 first --
    otherwise `upgrade` would start from base and fail on a duplicate
    `CREATE TABLE`. Fresh deploys skip this and build everything from 0001.
    """
    from alembic import command
    from alembic.config import Config

    lock_engine = create_engine(settings.database_url, poolclass=pool.NullPool, future=True)
    try:
        with lock_engine.connect() as conn:
            log.info("acquiring migration lock")
            conn.execute(
                text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATION_LOCK_KEY}
            )
            try:
                cfg = Config(str(_BACKEND_DIR / "alembic.ini"))
                cfg.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))

                insp = inspect(conn)
                if not insp.has_table("alembic_version") and insp.has_table("hosts"):
                    log.warning(
                        "baseline schema present without alembic_version; "
                        "stamping 0001 (legacy init.sql bootstrap)"
                    )
                    command.stamp(cfg, "0001")
                    configure_logging()

                log.info("running alembic upgrade head")
                command.upgrade(cfg, "head")
                configure_logging()  # command.upgrade's fileConfig replaced our handler
                log.info("migrations up to date")
            finally:
                conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": _MIGRATION_LOCK_KEY}
                )
    finally:
        lock_engine.dispose()


def main() -> None:
    configure_logging()
    # Alembic's fileConfig (run in-process by command.upgrade) resets the root
    # level; pin ours so the post-migration lines still come through.
    log.setLevel(logging.INFO)
    wait_for_db()
    run_migrations()


if __name__ == "__main__":
    main()
