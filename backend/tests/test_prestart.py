"""app.prestart.run_migrations against a real database.

- a fresh (empty) database is built straight from revision 0001;
- a legacy database bootstrapped from init.sql (baseline tables, no
  alembic_version) is stamped 0001 and upgraded instead of crashing on a
  duplicate CREATE TABLE.

Each test runs on its own throwaway database so the shared cadence_test DB is
untouched.
"""

from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import create_engine, inspect, text

from app.core.config import settings

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
INIT_SQL = (BACKEND_DIR / "app" / "db" / "init.sql").read_text()
HEAD_REVISION = "0014"


@pytest.fixture()
def throwaway_db(monkeypatch):
    """A brand-new empty database, with settings.database_url pointed at it for
    the test's duration. Dropped on teardown."""
    base, _, _ = settings.database_url.rpartition("/")
    name = "cadence_prestart_test"
    admin = create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = f"{base}/{name}"
    monkeypatch.setattr(settings, "database_url", url)
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _applied_revision(url: str) -> str | None:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            if not inspect(conn).has_table("alembic_version"):
                return None
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        engine.dispose()


def test_run_migrations_builds_a_fresh_database(throwaway_db):
    from app import prestart

    prestart.run_migrations()

    assert _applied_revision(throwaway_db) == HEAD_REVISION
    engine = create_engine(throwaway_db, future=True)
    try:
        with engine.connect() as conn:
            assert inspect(conn).has_table("hosts")
            assert inspect(conn).has_table("schedules")
    finally:
        engine.dispose()


def test_run_migrations_adopts_a_legacy_init_sql_database(throwaway_db):
    from app import prestart

    seed = create_engine(throwaway_db, future=True)
    try:
        with seed.begin() as conn:
            conn.exec_driver_sql(INIT_SQL)
        with seed.connect() as conn:
            assert inspect(conn).has_table("hosts")
            assert not inspect(conn).has_table("alembic_version")
    finally:
        seed.dispose()

    prestart.run_migrations()  # must not raise on the pre-existing baseline

    assert _applied_revision(throwaway_db) == HEAD_REVISION
