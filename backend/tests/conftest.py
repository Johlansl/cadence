"""Test fixtures.

A real PostgreSQL is required -- the code uses JSONB, gen_random_uuid(),
ON CONFLICT and FOR UPDATE SKIP LOCKED, none of which SQLite provides.

Connection: set TEST_DATABASE_URL, or let it be derived from the POSTGRES_*
variables (same ones the app reads) with the database name `cadence_test`.
The test database is dropped and recreated once per run by running the full
Alembic migration chain (`alembic upgrade head`), so the migrations are
exercised too. Each test runs inside a transaction that is rolled back on
teardown, so tests never see each other's rows.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import pathlib
import time

import httpx
import pytest
from cryptography.fernet import Fernet

# --- configuration, applied before the app package is imported ---------------

ADMIN_KEY = "test-admin-key"
os.environ["CADENCE_ADMIN_KEY"] = ADMIN_KEY
# The auth-failure throttle sleeps a worker thread; disable it so the negative
# auth tests stay fast. Covered directly in test_auth_throttle.py.
os.environ["CADENCE_DISABLE_AUTH_THROTTLE"] = "1"
# The successful-traffic rate limiter would 429 the many requests a single test
# fires. Off by default here; test_ratelimit.py re-enables it per test.
os.environ["CADENCE_RATELIMIT_ENABLED"] = "false"
# Most API tests exercise HMAC directly without a reverse proxy. Dedicated
# transport tests enable the Caddy marker checks explicitly.
os.environ["CADENCE_REQUIRE_AGENT_TRANSPORT_AUTH"] = "false"
# Fresh per test run; nothing needs it to persist across runs -- every test
# token is issued and used within the one run that generated it.
os.environ["CADENCE_TOKEN_ENCRYPTION_KEY"] = Fernet.generate_key().decode()


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

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def _prepare_database():
    """Drop + recreate the test database, then run all migrations."""
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    from alembic import command

    base, _, dbname = TEST_DATABASE_URL.rpartition("/")
    admin_engine = create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT", future=True)
    with admin_engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    admin_engine.dispose()

    # env.py reads the URL from settings.database_url, which conftest has
    # already pointed at TEST_DATABASE_URL via CADENCE_DATABASE_URL.
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")
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


class _SignedAgentAuth(httpx.Auth):
    """Sign a request the way agent 0.8.0+ does (app.api.deps._auth_signed):
    X-Cadence-Token-Hash / -Timestamp / -Signature, an HMAC-SHA256 over
    timestamp + method + path + sha256(body) keyed with the raw token."""

    requires_request_body = True

    def __init__(self, secret: str) -> None:
        self._secret = secret

    def auth_flow(self, request):
        ts = str(int(time.time()))
        body_hash = hashlib.sha256(request.content or b"").hexdigest()
        canonical = f"{ts}\n{request.method}\n{request.url.path}\n{body_hash}"
        sig = hmac.new(
            self._secret.encode(), canonical.encode(), hashlib.sha256
        ).hexdigest()
        request.headers["X-Cadence-Token-Hash"] = hashlib.sha256(
            self._secret.encode()
        ).hexdigest()
        request.headers["X-Cadence-Timestamp"] = ts
        request.headers["X-Cadence-Signature"] = sig
        yield request


def signed(token: str) -> _SignedAgentAuth:
    """Auth for agent-authenticated endpoints: `client.post(url, auth=signed(token), ...)`."""
    return _SignedAgentAuth(token)


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


def webhook_row(
    db,
    *,
    url: str = "https://example.test/hook",
    events: tuple[str, ...] = ("job.succeeded",),
    enabled: bool = True,
    secret: str = "whsec-test-secret",
    description: str | None = None,
):
    """Insert a webhook directly (the admin routes arrive in a later commit).
    Returns the flushed ORM object."""
    from app.core.crypto import encrypt_token_secret
    from app.models.models import Webhook

    hook = Webhook(
        url=url,
        secret_encrypted=encrypt_token_secret(secret),
        enabled=enabled,
        event_types=list(events),
        description=description,
    )
    db.add(hook)
    db.flush()
    return hook


def pkg(
    name: str,
    *,
    candidate: str | None = None,
    security: bool = False,
    source: str | None = None,
    architecture: str = "amd64",
) -> dict:
    row = {
        "name": name,
        "architecture": architecture,
        "installed_version": "1.0",
        "candidate_version": candidate,
        "is_security_update": security,
        "update_origin": "Debian-Security:13/stable-security" if security else "Debian:13/stable",
    }
    if source is not None:
        row["source_package"] = source
    return row
