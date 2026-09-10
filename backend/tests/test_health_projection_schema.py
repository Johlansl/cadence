from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.models.models import Host

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture()
def throwaway_db(monkeypatch):
    base, _, _ = settings.database_url.rpartition("/")
    name = "cadence_mig0018_test"
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


def _projection_columns(url: str) -> tuple[set[str], str]:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            columns = {column["name"] for column in inspect(conn).get_columns("hosts")}
            revision = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            return columns.intersection({"health_status", "health_checked_at"}), revision
    finally:
        engine.dispose()


def test_0018_up_down_up_round_trip(throwaway_db):
    from alembic import command

    cfg = _alembic_config()

    command.upgrade(cfg, "0018")
    assert _projection_columns(throwaway_db) == (
        {"health_status", "health_checked_at"},
        "0018",
    )

    command.downgrade(cfg, "0017")
    assert _projection_columns(throwaway_db) == (set(), "0017")

    command.upgrade(cfg, "0018")
    assert _projection_columns(throwaway_db)[1] == "0018"


def test_hosts_health_projection_columns_and_check_exist(db_session):
    inspector = inspect(db_session.connection())

    columns = {column["name"] for column in inspector.get_columns("hosts")}
    constraints = {
        constraint["name"] for constraint in inspector.get_check_constraints("hosts")
    }

    assert {"health_status", "health_checked_at"} <= columns
    assert "hosts_health_status_check" in constraints


def test_hosts_health_status_check_rejects_unknown_values(db_session):
    host = Host(hostname="vm-health-check")
    db_session.add(host)
    db_session.flush()

    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(
                text("UPDATE hosts SET health_status = 'broken' WHERE id = :id"),
                {"id": host.id},
            )
