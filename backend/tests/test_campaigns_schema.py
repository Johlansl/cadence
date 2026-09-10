"""Migration 0016 (campaigns): up/down/up round-trip, and every table CHECK.

The round-trip builds its own throwaway database (like test_prestart.py) and
drives alembic to head, back to 0015, and up again. The CHECK tests run against
the rolled-back cadence_test session; each expected rejection is wrapped in a
SAVEPOINT so the session stays usable for the next assertion.
"""

from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.models.models import Campaign, Host

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]


# --- migration up / down / up round-trip -------------------------------------


@pytest.fixture()
def throwaway_db(monkeypatch):
    """A brand-new empty database with settings.database_url pointed at it for
    the test's duration; dropped on teardown. Mirrors test_prestart.py."""
    base, _, _ = settings.database_url.rpartition("/")
    name = "cadence_mig0016_test"
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
                "campaigns": insp.has_table("campaigns"),
                "campaign_hosts": insp.has_table("campaign_hosts"),
                "jobs_campaign_id": "campaign_id"
                in {c["name"] for c in insp.get_columns("jobs")},
                "jobs_job_type_check": "jobs_job_type_check"
                in {c["name"] for c in insp.get_check_constraints("jobs")},
                "revision": conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one(),
            }
    finally:
        eng.dispose()


def test_0016_up_down_up_round_trip(throwaway_db):
    from alembic import command

    cfg = _alembic_config()

    command.upgrade(cfg, "head")
    assert _reflect(throwaway_db) == {
        "campaigns": True,
        "campaign_hosts": True,
        "jobs_campaign_id": True,
        "jobs_job_type_check": True,
        "revision": "0016",
    }

    command.downgrade(cfg, "0015")
    assert _reflect(throwaway_db) == {
        "campaigns": False,
        "campaign_hosts": False,
        "jobs_campaign_id": False,
        "jobs_job_type_check": False,
        "revision": "0015",
    }

    command.upgrade(cfg, "head")  # re-upgrading a downgraded DB must be clean
    assert _reflect(throwaway_db)["revision"] == "0016"


# --- table CHECK constraints ------------------------------------------------


def _rejects(db_session, sql: str, params: dict | None = None) -> None:
    """Assert `sql` trips a CHECK / integrity error, inside a SAVEPOINT so the
    session is still usable afterwards."""
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(text(sql), params or {})


def _host(db_session, hostname: str = "vm-check") -> str:
    h = Host(hostname=hostname)
    db_session.add(h)
    db_session.flush()
    return str(h.id)


def _campaign(db_session) -> str:
    c = Campaign(
        name="check-campaign",
        stages=[1],
        max_concurrency=1,
        max_failures=0,
        observation_window_seconds=600,
    )
    db_session.add(c)
    db_session.flush()
    return str(c.id)


_INSERT_CH = (
    "INSERT INTO campaign_hosts "
    "(campaign_id, host_id, stage_index, state, skip_reason) "
    "VALUES (:c, :h, :stage, :state, :reason)"
)


def test_jobs_job_type_check_accepts_the_three_known_types(db_session):
    host_id = _host(db_session)
    for jt in ("apt_upgrade", "reboot", "apt_dry_run"):
        db_session.execute(
            text("INSERT INTO jobs (host_id, job_type) VALUES (:h, :jt)"),
            {"h": host_id, "jt": jt},
        )
    db_session.flush()


def test_jobs_job_type_check_rejects_an_unknown_type(db_session):
    host_id = _host(db_session)
    _rejects(
        db_session,
        "INSERT INTO jobs (host_id, job_type) VALUES (:h, 'campaign_upgrade')",
        {"h": host_id},
    )


def test_campaigns_status_check_accepts_every_declared_status(db_session):
    for st in ("draft", "running", "paused", "completed", "stopped", "cancelled"):
        db_session.execute(
            text(
                "INSERT INTO campaigns (name, stages, max_concurrency, max_failures, "
                "observation_window_seconds, status) "
                "VALUES ('c', '[1]'::jsonb, 1, 0, 600, :st)"
            ),
            {"st": st},
        )
    db_session.flush()


def test_campaigns_status_check_rejects_an_unknown_status(db_session):
    _rejects(
        db_session,
        "INSERT INTO campaigns (name, stages, max_concurrency, max_failures, "
        "observation_window_seconds, status) "
        "VALUES ('c', '[1]'::jsonb, 1, 0, 600, 'halted')",
    )


def test_campaigns_numeric_checks_reject_out_of_range(db_session):
    base = (
        "INSERT INTO campaigns (name, stages, max_concurrency, max_failures, "
        "observation_window_seconds) VALUES ('c', '[1]'::jsonb, {}, {}, {})"
    )
    _rejects(db_session, base.format(0, 0, 600))  # max_concurrency >= 1
    _rejects(db_session, base.format(1, -1, 600))  # max_failures >= 0
    _rejects(db_session, base.format(1, 0, -1))  # observation_window_seconds >= 0


def test_campaign_hosts_state_check_rejects_an_unknown_state(db_session):
    cid, hid = _campaign(db_session), _host(db_session)
    _rejects(
        db_session,
        _INSERT_CH,
        {"c": cid, "h": hid, "stage": 0, "state": "aborted", "reason": None},
    )


def test_campaign_hosts_state_check_accepts_every_declared_state(db_session):
    cid = _campaign(db_session)
    for i, st in enumerate(("pending", "running", "done", "skipped", "orphaned")):
        db_session.execute(
            text(_INSERT_CH),
            {
                "c": cid,
                "h": _host(db_session, f"vm-state-{i}"),
                "stage": 0,
                "state": st,
                "reason": "apt_locked" if st == "skipped" else None,
            },
        )
    db_session.flush()


def test_campaign_hosts_stage_check_rejects_a_negative_stage(db_session):
    cid, hid = _campaign(db_session), _host(db_session)
    _rejects(
        db_session,
        _INSERT_CH,
        {"c": cid, "h": hid, "stage": -1, "state": "pending", "reason": None},
    )


def test_campaign_hosts_skip_reason_biconditional(db_session):
    cid = _campaign(db_session)

    # skipped WITH a reason -> ok
    db_session.execute(
        text(_INSERT_CH),
        {
            "c": cid,
            "h": _host(db_session, "vm-skip-ok"),
            "stage": 0,
            "state": "skipped",
            "reason": "apt_locked",
        },
    )
    # a non-skipped state WITHOUT a reason -> ok
    db_session.execute(
        text(_INSERT_CH),
        {
            "c": cid,
            "h": _host(db_session, "vm-done-ok"),
            "stage": 0,
            "state": "done",
            "reason": None,
        },
    )
    db_session.flush()

    # skipped WITHOUT a reason -> rejected (forward direction)
    _rejects(
        db_session,
        _INSERT_CH,
        {
            "c": cid,
            "h": _host(db_session, "vm-skip-noreason"),
            "stage": 1,
            "state": "skipped",
            "reason": None,
        },
    )
    # a reason on a non-skipped state -> rejected (reverse direction)
    _rejects(
        db_session,
        _INSERT_CH,
        {
            "c": cid,
            "h": _host(db_session, "vm-orphaned-reason"),
            "stage": 1,
            "state": "orphaned",
            "reason": "apt_locked",
        },
    )
