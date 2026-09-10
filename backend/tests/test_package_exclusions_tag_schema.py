"""Migration 0017 (package_exclusions.tag): up/down/up round-trip, and the
widened scope / coherence CHECK constraints.

The round-trip builds its own throwaway database (like test_campaigns_schema.py)
and drives alembic to 0017, back to 0016, and up to 0017 again. The CHECK tests
run against the rolled-back cadence_test session; each expected rejection is
wrapped in a SAVEPOINT so the session stays usable for the next assertion.
"""

from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture()
def throwaway_db(monkeypatch):
    """A brand-new empty database with settings.database_url pointed at it for
    the test's duration; dropped on teardown. Mirrors test_campaigns_schema.py."""
    base, _, _ = settings.database_url.rpartition("/")
    name = "cadence_mig0017_test"
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


def _alembic_config():
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


def _reflect(url: str) -> dict:
    eng = create_engine(url, future=True)
    try:
        with eng.connect() as conn:
            insp = inspect(conn)
            return {
                "tag_column": "tag"
                in {c["name"] for c in insp.get_columns("package_exclusions")},
                "revision": conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one(),
            }
    finally:
        eng.dispose()


def test_0017_up_down_up_round_trip(throwaway_db):
    from alembic import command

    cfg = _alembic_config()

    command.upgrade(cfg, "0017")
    assert _reflect(throwaway_db) == {"tag_column": True, "revision": "0017"}

    command.downgrade(cfg, "0016")
    assert _reflect(throwaway_db) == {"tag_column": False, "revision": "0016"}

    command.upgrade(cfg, "0017")  # re-upgrading a downgraded DB must be clean
    assert _reflect(throwaway_db)["revision"] == "0017"


# --- widened CHECK constraints --------------------------------------------


def _rejects(db_session, sql: str, params: dict | None = None) -> None:
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(text(sql), params or {})


_INSERT = (
    "INSERT INTO package_exclusions (scope, host_id, tag, pattern) "
    "VALUES (:scope, :host_id, :tag, :pattern)"
)


def _host(db_session, hostname: str = "vm-pe-check") -> str:
    from app.models.models import Host

    h = Host(hostname=hostname)
    db_session.add(h)
    db_session.flush()
    return str(h.id)


def test_scope_check_accepts_global_host_tag(db_session):
    hid = _host(db_session)
    rows = [
        {"scope": "global", "host_id": None, "tag": None, "pattern": "a*"},
        {"scope": "host", "host_id": hid, "tag": None, "pattern": "b*"},
        {"scope": "tag", "host_id": None, "tag": "role=web", "pattern": "c*"},
    ]
    for r in rows:
        db_session.execute(text(_INSERT), r)
    db_session.flush()


def test_scope_check_rejects_an_unknown_scope(db_session):
    _rejects(
        db_session,
        _INSERT,
        {"scope": "cluster", "host_id": None, "tag": None, "pattern": "a*"},
    )


def test_coherence_rejects_tag_scope_without_a_tag(db_session):
    _rejects(
        db_session,
        _INSERT,
        {"scope": "tag", "host_id": None, "tag": None, "pattern": "a*"},
    )


def test_coherence_rejects_a_blank_tag(db_session):
    for blank in ("", "   "):
        _rejects(
            db_session,
            _INSERT,
            {"scope": "tag", "host_id": None, "tag": blank, "pattern": "a*"},
        )


def test_coherence_rejects_tag_scope_carrying_a_host_id(db_session):
    hid = _host(db_session)
    _rejects(
        db_session,
        _INSERT,
        {"scope": "tag", "host_id": hid, "tag": "role=web", "pattern": "a*"},
    )


def test_coherence_rejects_global_or_host_scope_carrying_a_tag(db_session):
    hid = _host(db_session)
    _rejects(
        db_session,
        _INSERT,
        {"scope": "global", "host_id": None, "tag": "role=web", "pattern": "a*"},
    )
    _rejects(
        db_session,
        _INSERT,
        {"scope": "host", "host_id": hid, "tag": "role=web", "pattern": "a*"},
    )
