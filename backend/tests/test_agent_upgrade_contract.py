"""The closed agent_upgrade contract (6B, server side only).

POST /api/v1/admin/hosts/{id}/jobs with job_type agent_upgrade accepts
exactly {"target_version": "N.N.N"}: strict grammar, no extra keys (no url,
checksum, pubkey, path, command, allow_downgrade), no silent coercion.
Creation, claim, audit and the one-active-job guard all reuse the generic
job paths; create_job_for_host injects nothing for this type. No agent
executes it yet (current agents refuse it as an unknown type).

Migration 0026 widens jobs.job_type CHECK only: up/down/up round-trip on a
throwaway DB, pinned to 0026, mirroring test_campaigns_schema.py.
"""

from __future__ import annotations

import pathlib
import uuid

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.job_creation import create_job_for_host
from app.models.models import AuditLog, Host, Job
from tests.conftest import ADMIN_HEADERS, create_host, signed

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]

VALID = {"job_type": "agent_upgrade", "params": {"target_version": "0.15.0"}}

INVALID_TARGETS = [
    "v0.15.0",
    "0.15",
    "0.15.0.1",
    "0.15.0-rc1",
    "0.15.0+meta",
    " 0.15.0",
    "0.15.0 ",
    "",
    "latest",
    "0.15.0\n",
    "01.15.0",  # not canonical: leading zeros refused (6B, PR6)
    "0.15.00",
    "00.0.0",
    "1" * 200,  # absurd length, also not N.N.N
]

TRAP_KEYS = [
    "url",
    "sha256",
    "checksum",
    "pubkey",
    "path",
    "command",
    "cmd",
    "args",
    "base_url",
    "allow_downgrade",
]


def _create(client, host_id: str, payload: dict):
    return client.post(
        f"/api/v1/admin/hosts/{host_id}/jobs", headers=ADMIN_HEADERS, json=payload
    )


def test_create_valid_reports_exact_params_and_audits(client, db_session):
    host_id, _token = create_host(client)

    r = _create(client, host_id, VALID)

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["job_type"] == "agent_upgrade"
    assert body["params"] == {"target_version": "0.15.0"}
    job = db_session.get(Job, body["id"])
    assert job.params == {"target_version": "0.15.0"}
    assert job.status == "pending"
    row = db_session.execute(
        select(AuditLog)
        .where(AuditLog.action == "job.create", AuditLog.target_id == body["id"])
        .order_by(AuditLog.id.desc())
        .limit(1)
    ).scalar_one()
    assert row.detail["host_id"] == host_id
    assert row.detail["job_type"] == "agent_upgrade"
    assert row.detail["params"] == {"target_version": "0.15.0"}


@pytest.mark.parametrize("target", ["0.15.0", "1.0.0", "10.20.30"])
def test_valid_targets_accepted(client, target):
    host_id, _token = create_host(client)

    r = _create(client, host_id, {"job_type": "agent_upgrade", "params": {"target_version": target}})

    assert r.status_code == 201, r.text


@pytest.mark.parametrize("target", INVALID_TARGETS)
def test_invalid_targets_rejected_422(client, target):
    host_id, _token = create_host(client)

    r = _create(client, host_id, {"job_type": "agent_upgrade", "params": {"target_version": target}})

    assert r.status_code == 422, (target, r.text)


@pytest.mark.parametrize("target", [15, 0.15, True, None, ["0.15.0"], {"v": "0.15.0"}])
def test_non_string_targets_rejected_without_coercion(client, target):
    host_id, _token = create_host(client)

    r = _create(client, host_id, {"job_type": "agent_upgrade", "params": {"target_version": target}})

    assert r.status_code == 422, (target, r.text)


def test_missing_target_rejected(client):
    host_id, _token = create_host(client)

    assert _create(client, host_id, {"job_type": "agent_upgrade", "params": {}}).status_code == 422
    assert _create(client, host_id, {"job_type": "agent_upgrade"}).status_code == 422


@pytest.mark.parametrize("key", TRAP_KEYS)
def test_extra_keys_rejected_never_ignored(client, db_session, key):
    host_id, _token = create_host(client)
    params = {"target_version": "0.15.0", key: "x"}

    r = _create(client, host_id, {"job_type": "agent_upgrade", "params": params})

    assert r.status_code == 422, (key, r.text)
    assert db_session.execute(select(func.count()).select_from(Job)).scalar() == 0


def test_other_job_types_accept_extra_params_unchanged(client):
    # The closed contract applies to agent_upgrade only; the apt types keep
    # their free-form params (e.g. reboot overrides still validate).
    host_id, _token = create_host(client)

    r = _create(
        client,
        host_id,
        {"job_type": "apt_dry_run", "params": {"reboot": "never", "note": "free"}},
    )

    assert r.status_code == 201, r.text


def test_create_job_for_host_injects_nothing(db_session):
    host = Host(hostname="vm-up")
    db_session.add(host)
    db_session.flush()

    job = create_job_for_host(
        db_session,
        host_id=host.id,
        job_type="agent_upgrade",
        params={"target_version": "0.15.0"},
    )

    assert job is not None
    assert job.params == {"target_version": "0.15.0"}


def test_claim_returns_params_intact_with_no_reboot_pin(client, db_session):
    host_id, token = create_host(client)
    assert _create(client, host_id, VALID).status_code == 201

    r = client.post("/api/v1/agent/next-job", auth=signed(token))

    assert r.status_code == 200
    handoff = r.json()["job"]
    assert handoff["job_type"] == "agent_upgrade"
    assert handoff["params"] == {"target_version": "0.15.0"}
    job = db_session.execute(
        select(Job).where(Job.host_id == uuid.UUID(host_id))
    ).scalar_one()
    assert job.status == "running"
    assert job.started_at is not None
    assert job.params == {"target_version": "0.15.0"}


def test_active_guard_both_directions(client):
    host_id, _token = create_host(client, hostname="vm-guard")
    assert _create(client, host_id, VALID).status_code == 201

    # An active agent_upgrade blocks any other job, like any active job.
    assert _create(client, host_id, {"job_type": "apt_dry_run"}).status_code == 409

    host2_id, _token2 = create_host(client, hostname="vm-guard2")
    assert _create(client, host2_id, {"job_type": "apt_dry_run"}).status_code == 201

    # And an active job of another type blocks agent_upgrade.
    assert _create(client, host2_id, VALID).status_code == 409


def test_db_check_rejects_unknown_job_type_at_head(db_session):
    host = Host(hostname="vm-check")
    db_session.add(host)
    db_session.flush()
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.execute(
                text("INSERT INTO jobs (host_id, job_type) VALUES (:h, 'nope')"),
                {"h": str(host.id)},
            )


# --- migration 0026 up / down / up round-trip -------------------------------


@pytest.fixture()
def throwaway_db(monkeypatch):
    from app.core.config import settings

    base, _, _ = settings.database_url.rpartition("/")
    name = "cadence_mig0026_test"
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


def _check_text(url: str) -> str:
    eng = create_engine(url, future=True)
    try:
        with eng.connect() as conn:
            insp = inspect(conn)
            checks = {
                c["name"]: c["sqltext"] for c in insp.get_check_constraints("jobs")
            }
            return str(checks["jobs_job_type_check"])
    finally:
        eng.dispose()


def _insert_job_type(url: str, job_type: str) -> None:
    eng = create_engine(url, future=True)
    try:
        with eng.begin() as conn:
            host_id = conn.execute(
                text("INSERT INTO hosts (hostname) VALUES ('vm-mig') RETURNING id")
            ).scalar_one()
            conn.execute(
                text("INSERT INTO jobs (host_id, job_type) VALUES (:h, :t)"),
                {"h": str(host_id), "t": job_type},
            )
    finally:
        eng.dispose()


def test_0026_up_down_up_round_trip(throwaway_db):
    from alembic import command

    cfg = _alembic_config()

    command.upgrade(cfg, "0026")
    assert "agent_upgrade" in _check_text(throwaway_db)
    _insert_job_type(throwaway_db, "agent_upgrade")

    # Downgrade does not delete rows: a surviving agent_upgrade row would
    # fail the narrower CHECK (documented in 0026). Clear the probe first,
    # like an operator would clear real rows before rolling back.
    eng = create_engine(throwaway_db, future=True)
    try:
        with eng.begin() as conn:
            conn.execute(text("DELETE FROM jobs WHERE job_type = 'agent_upgrade'"))
    finally:
        eng.dispose()

    command.downgrade(cfg, "0025")
    assert "agent_upgrade" not in _check_text(throwaway_db)
    with pytest.raises(IntegrityError):
        _insert_job_type(throwaway_db, "agent_upgrade")

    command.upgrade(cfg, "0026")  # re-upgrading a downgraded DB must be clean
    assert "agent_upgrade" in _check_text(throwaway_db)
    _insert_job_type(throwaway_db, "agent_upgrade")
