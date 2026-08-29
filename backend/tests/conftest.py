"""Test fixtures.

A real PostgreSQL is required -- the code uses JSONB, gen_random_uuid(),
ON CONFLICT and FOR UPDATE SKIP LOCKED, none of which SQLite provides.

Connection: set TEST_DATABASE_URL, or let it be derived from the POSTGRES_*
variables (same ones the app reads) with the database name `cadence_test`.
The test database is dropped and recreated from app/db/init.sql once per run.
Each test runs inside a transaction that is rolled back on teardown, so tests
never see each other's rows.
"""

from __future__ import annotations

import os
import pathlib

import pytest

# --- configuration, applied before the app package is imported ---------------

ADMIN_KEY = "test-admin-key"
os.environ["CADENCE_ADMIN_KEY"] = ADMIN_KEY


def _test_database_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    user = os.environ.get("POSTGRES_USER", "cadence")
    password = os.environ.get("POSTGRES_PASSWORD", "cadence")
    host = os.environ.get("POSTGRES_HOST", "db")
    port = os.environ.get("POSTGRES_PORT", "5432")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/cadence_test"


TEST_DATABASE_URL = _test_database_url()
# The app builds its engine from this at import time.
os.environ["CADENCE_DATABASE_URL"] = TEST_DATABASE_URL

INIT_SQL = pathlib.Path(__file__).resolve().parents[1] / "app" / "db" / "init.sql"


@pytest.fixture(scope="session", autouse=True)
def _prepare_database():
    """Drop + recreate the test database and apply the schema."""
    from sqlalchemy import create_engine, text

    base, _, dbname = TEST_DATABASE_URL.rpartition("/")
    admin_engine = create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT", future=True)
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    admin_engine.dispose()

    schema_engine = create_engine(TEST_DATABASE_URL, future=True)
    with schema_engine.begin() as conn:
        # psycopg2 executes the whole multi-statement script in one call.
        conn.exec_driver_sql(INIT_SQL.read_text())
    schema_engine.dispose()
    yield


@pytest.fixture()
def db_session():
    """A session bound to a single connection wrapped in an outer transaction.
    Nested savepoints are restarted after each inner commit so that code under
    test can call commit() freely; everything is rolled back at the end."""
    from sqlalchemy import event
    from sqlalchemy.orm import sessionmaker

    from app.db.base import engine

    connection = engine.connect()
    outer = connection.begin()
    Session = sessionmaker(
        bind=connection, autoflush=False, expire_on_commit=False, future=True
    )
    session = Session()
    session.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def _restart_savepoint(sess, trans):  # noqa: ANN001
        if trans.nested and not trans._parent.nested:
            sess.begin_nested()

    try:
        yield session
    finally:
        event.remove(session, "after_transaction_end", _restart_savepoint)
        session.close()
        outer.rollback()
        connection.close()


@pytest.fixture()
def client(db_session):
    """TestClient with get_db overridden to the rolled-back session."""
    from fastapi.testclient import TestClient

    from app.api.deps import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db_session
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_db, None)


# --- helpers ---------------------------------------------------------------

ADMIN_HEADERS = {"X-Admin-Key": ADMIN_KEY}


def create_host(client, hostname: str = "vm-test") -> tuple[str, str]:
    """Provision a host through the API. Returns (host_id, token)."""
    r = client.post(
        "/api/v1/admin/hosts", headers=ADMIN_HEADERS, json={"hostname": hostname}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    return body["id"], body["token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def report_payload(**overrides) -> dict:
    payload = {
        "agent_version": "test",
        "hostname": "vm-test",
        "os_family": "debian",
        "os_name": "Debian GNU/Linux",
        "os_version": "13",
        "package_manager": "apt",
        "reboot_required": False,
        "packages": [],
    }
    payload.update(overrides)
    return payload


def pkg(name: str, *, candidate: str | None = None, security: bool = False) -> dict:
    return {
        "name": name,
        "architecture": "amd64",
        "installed_version": "1.0",
        "candidate_version": candidate,
        "is_security_update": security,
        "update_origin": "Debian-Security:13/stable-security" if security else "Debian:13/stable",
    }
