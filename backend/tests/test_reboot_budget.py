"""Global reboot budget (6A lot 4) at the shared creation path.

One admission rule for campaigns, schedules and manual jobs: reboot-capable
creations (dedicated reboots, auto upgrades) are refused past the cap, while
read-only types and a disabled budget behave exactly as before. The threaded
test uses real PostgreSQL transactions, never mocks.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.campaigns.engine import advance_campaigns
from app.core.config import settings
from app.db.base import engine
from app.job_creation import create_job_for_host
from app.models.models import CampaignHost, Host, Job, Schedule
from app.scheduler import tick
from tests.conftest import ADMIN_HEADERS, create_host, report_payload, signed

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _budget(monkeypatch, n):
    monkeypatch.setattr(settings, "max_concurrent_reboots", n)


def _auto_host(client, hostname):
    host_id, token = create_host(client, hostname=hostname)
    r = client.patch(
        f"/api/v1/admin/hosts/{host_id}",
        headers=ADMIN_HEADERS,
        json={"reboot_policy": "auto"},
    )
    assert r.status_code == 200, r.text
    return host_id, token


def _draft(client, host_ids, **kw):
    body = {
        "name": kw.get("name", "eng"),
        "host_ids": host_ids,
        "stages": kw.get("stages", ["rest"]),
        "max_concurrency": kw.get("max_concurrency", 1),
        "max_failures": kw.get("max_failures", 0),
    }
    r = client.post("/api/v1/admin/campaigns", headers=ADMIN_HEADERS, json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _activate(client, cid):
    r = client.post(f"/api/v1/admin/campaigns/{cid}/activate", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text


def _row(db_session, cid, host_id):
    return db_session.execute(
        select(CampaignHost).where(
            CampaignHost.campaign_id == uuid.UUID(cid),
            CampaignHost.host_id == host_id,
        )
    ).scalar_one()


def _terminal(db_session, job_id, at):
    job = db_session.get(Job, job_id)
    job.status = "succeeded"
    job.completed_at = at
    job.result = {"health_status": "healthy"}
    db_session.flush()


def _manual(client, host_id, **body):
    return client.post(
        f"/api/v1/admin/hosts/{host_id}/jobs", headers=ADMIN_HEADERS, json=body
    )


def _due_schedule(db_session, host_id, params=None):
    sched = Schedule(
        host_id=host_id,
        enabled=True,
        kind="weekly",
        weekday=0,
        hour=3,
        minute=0,
        timezone="UTC",
        params=params or {},
        next_run_at=NOW - timedelta(minutes=1),
    )
    db_session.add(sched)
    db_session.flush()
    return sched


def test_budget_one_serializes_two_campaigns(client, db_session, monkeypatch):
    _budget(monkeypatch, 1)
    aide, _ = _auto_host(client, "a")
    bide, _ = _auto_host(client, "b")
    ca, cb = _draft(client, [aide], name="a"), _draft(client, [bide], name="b")

    _activate(client, ca)
    advance_campaigns(now=NOW, db=db_session)
    assert _row(db_session, ca, aide).state == "running"

    _activate(client, cb)
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _row(db_session, cb, bide).state == "pending"  # budget held by A
    assert _row(db_session, cb, bide).job_id is None

    _terminal(db_session, _row(db_session, ca, aide).job_id, NOW)
    advance_campaigns(now=NOW + timedelta(seconds=2), db=db_session)
    advance_campaigns(now=NOW + timedelta(seconds=3), db=db_session)
    assert _row(db_session, cb, bide).state == "running"  # released on terminal


def test_awaited_row_holds_budget_until_proven(client, db_session, monkeypatch):
    _budget(monkeypatch, 1)
    aide, _ = _auto_host(client, "a")
    bide, _ = _auto_host(client, "b")
    ca = _draft(client, [aide])
    _activate(client, ca)
    advance_campaigns(now=NOW, db=db_session)

    job = db_session.get(Job, _row(db_session, ca, aide).job_id)
    job.status = "succeeded"
    job.completed_at = NOW
    job.result = {"health_status": "healthy", "boot_id": "boot-1", "will_reboot": True}
    db_session.flush()
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _row(db_session, ca, aide).awaited_boot == "boot-1"

    assert _manual(client, bide).status_code == 409  # await holds the slot

    observed = db_session.get(Host, aide)
    observed.current_boot_id = "boot-2"
    observed.health_status = "healthy"
    observed.health_checked_at = NOW + timedelta(seconds=2)
    db_session.flush()
    advance_campaigns(now=NOW + timedelta(seconds=3), db=db_session)
    assert _row(db_session, ca, aide).state == "done"

    assert _manual(client, bide).status_code == 201  # released by proof


def test_concurrent_admission_yields_exactly_one_job(monkeypatch):
    _budget(monkeypatch, 1)
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    setup = maker()
    try:
        hosts = [
            Host(hostname=f"vm-budget-{tag}", reboot_policy="auto") for tag in ("a", "b")
        ]
        setup.add_all(hosts)
        setup.flush()
        ids = [h.id for h in hosts]
        setup.commit()
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}

    def _create(tag: str, host_id) -> None:
        db = maker()
        try:
            barrier.wait(timeout=30)
            outcomes[tag] = create_job_for_host(db, host_id=host_id)
            db.commit()
        finally:
            db.close()

    try:
        threads = [
            threading.Thread(target=_create, args=(tag, hid), daemon=True)
            for tag, hid in zip(("a", "b"), ids, strict=True)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert all(not thread.is_alive() for thread in threads), outcomes
        assert sum(1 for job in outcomes.values() if job is not None) == 1
        assert sum(1 for job in outcomes.values() if job is None) == 1

        check = maker()
        try:
            assert check.query(Job).filter(Job.host_id.in_(ids)).count() == 1
        finally:
            check.close()
    finally:
        cleanup = maker()
        try:
            cleanup.query(Job).filter(Job.host_id.in_(ids)).delete()
            cleanup.query(Host).filter(Host.id.in_(ids)).delete()
            cleanup.commit()
        finally:
            cleanup.close()


def test_read_only_jobs_bypass_budget(client, db_session, monkeypatch):
    _budget(monkeypatch, 1)
    aide, _ = _auto_host(client, "a")
    bide, _ = _auto_host(client, "b")
    cide, _ = _auto_host(client, "c")

    assert _manual(client, aide).status_code == 201  # holds the only slot
    assert _manual(client, bide, job_type="health_check").status_code == 201
    assert _manual(client, cide, job_type="apt_dry_run").status_code == 201


def test_schedule_and_manual_share_budget(client, db_session, monkeypatch):
    _budget(monkeypatch, 1)
    aide, _ = _auto_host(client, "a")
    bide, _ = _auto_host(client, "b")
    cide, _ = create_host(client, hostname="c")

    assert _manual(client, cide, job_type="reboot").status_code == 201  # holds

    sched = _due_schedule(db_session, bide)
    assert tick(now=NOW, db=db_session) == 0  # refused, window still advances
    db_session.refresh(sched)
    assert sched.next_run_at > NOW - timedelta(minutes=1)
    assert _manual(client, aide).status_code == 409

    reboot = db_session.execute(select(Job).where(Job.host_id == cide)).scalar_one()
    _terminal(db_session, reboot.id, NOW)
    # A terminal reboot with no proven return still holds: only an explicit
    # recovery (here wiping the host's job history) releases it.
    r = client.delete(f"/api/v1/admin/hosts/{cide}/jobs", headers=ADMIN_HEADERS)
    assert r.status_code == 204
    db_session.refresh(sched)
    later = sched.next_run_at + timedelta(seconds=1)
    assert tick(now=later, db=db_session) == 1  # released, schedule runs


def test_never_policy_jobs_ignore_budget(client, db_session, monkeypatch):
    _budget(monkeypatch, 1)
    aide, _ = _auto_host(client, "a")
    bide, _ = create_host(client, hostname="b")  # policy defaults to never

    assert _manual(client, aide).status_code == 201  # holds the only slot
    assert _manual(client, bide).status_code == 201  # never: not counted


def test_disabled_budget_allows_concurrent_auto(client, db_session, monkeypatch):
    _budget(monkeypatch, 0)
    aide, _ = _auto_host(client, "a")
    bide, _ = _auto_host(client, "b")
    ca, cb = _draft(client, [aide], name="a"), _draft(client, [bide], name="b")
    _activate(client, ca)
    _activate(client, cb)

    advance_campaigns(now=NOW, db=db_session)
    assert _row(db_session, ca, aide).state == "running"
    assert _row(db_session, cb, bide).state == "running"


def test_pause_holds_and_cancel_keeps_job_hold_until_delete(
    client, db_session, monkeypatch
):
    _budget(monkeypatch, 1)
    aide, _ = _auto_host(client, "a")
    bide, _ = _auto_host(client, "b")
    ca = _draft(client, [aide])
    _activate(client, ca)
    advance_campaigns(now=NOW, db=db_session)

    job = db_session.get(Job, _row(db_session, ca, aide).job_id)
    job.status = "succeeded"
    job.completed_at = NOW
    job.result = {"health_status": "healthy", "boot_id": "boot-1", "will_reboot": True}
    db_session.flush()
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _row(db_session, ca, aide).awaited_boot == "boot-1"

    r = client.post(f"/api/v1/admin/campaigns/{ca}/pause", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert _manual(client, bide).status_code == 409  # still held while paused

    r = client.post(f"/api/v1/admin/campaigns/{ca}/cancel", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert _row(db_session, ca, aide).state == "orphaned"
    # Cancel releases the campaign row, but the unproven reboot still holds.
    assert _manual(client, bide).status_code == 409

    r = client.delete(f"/api/v1/admin/hosts/{aide}/jobs", headers=ADMIN_HEADERS)
    assert r.status_code == 204
    assert _manual(client, bide).status_code == 201  # released by recovery


def test_delete_recovers_pending_held_slot(client, db_session, monkeypatch):
    _budget(monkeypatch, 1)
    aide, _ = _auto_host(client, "a")
    bide, _ = _auto_host(client, "b")

    assert _manual(client, aide).status_code == 201
    assert _manual(client, bide).status_code == 409

    r = client.delete(f"/api/v1/admin/hosts/{aide}/jobs", headers=ADMIN_HEADERS)
    assert r.status_code == 204
    assert _manual(client, bide).status_code == 201


def test_terminal_unproven_holds_budget_before_reconcile(
    client, db_session, monkeypatch
):
    _budget(monkeypatch, 1)
    aide, _ = _auto_host(client, "a")
    bide, _ = _auto_host(client, "b")
    ca = _draft(client, [aide])
    _activate(client, ca)
    advance_campaigns(now=NOW, db=db_session)

    job = db_session.get(Job, _row(db_session, ca, aide).job_id)
    job.status = "succeeded"
    # Real clock: the terminal hold is bounded by the return timeout.
    job.completed_at = datetime.now(timezone.utc)
    job.result = {"health_status": "healthy", "boot_id": "boot-1", "will_reboot": True}
    db_session.flush()

    # The window: job terminal, reconcile not run yet, no await set.
    row = _row(db_session, ca, aide)
    assert row.state == "running" and row.awaited_boot is None
    assert _manual(client, bide).status_code == 409

    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _row(db_session, ca, aide).awaited_boot == "boot-1"
    assert _manual(client, bide).status_code == 409

    observed = db_session.get(Host, aide)
    observed.current_boot_id = "boot-2"
    observed.health_status = "healthy"
    observed.health_checked_at = NOW + timedelta(seconds=2)
    db_session.flush()
    advance_campaigns(now=NOW + timedelta(seconds=3), db=db_session)
    assert _row(db_session, ca, aide).state == "done"
    assert _manual(client, bide).status_code == 201


def test_non_campaign_reboot_tracked_until_boot_change(
    client, db_session, monkeypatch
):
    _budget(monkeypatch, 1)
    aide, atoken = _auto_host(client, "a")
    bide, _ = _auto_host(client, "b")

    job_id = _manual(client, aide).json()["id"]
    r = client.post("/api/v1/agent/next-job", auth=signed(atoken))
    assert r.status_code == 200 and r.json()["job"]["id"] == job_id
    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(atoken),
        json={
            "status": "succeeded",
            "exit_code": 0,
            "log": "ok",
            "boot_id": "boot-1",
            "will_reboot": True,
            "reboot_required": True,
        },
    )
    assert r.status_code == 200, r.text
    assert db_session.get(Host, aide).current_boot_id == "boot-1"

    assert _manual(client, bide).status_code == 409  # reboot unproven

    r = client.post(
        "/api/v1/reports", auth=signed(atoken), json=report_payload(boot_id="boot-2")
    )
    assert r.status_code == 200, r.text
    assert _manual(client, bide).status_code == 201  # back on a new boot


def _strip_pin(db_session, job_id):
    """Simulate a row created before reboot-mode pinning: no reboot key."""
    job = db_session.get(Job, job_id)
    params = dict(job.params)
    del params["reboot"]
    job.params = params
    db_session.flush()


def test_timeout_alone_never_releases_unproven_hold(client, db_session, monkeypatch):
    _budget(monkeypatch, 1)
    aide, atoken = _auto_host(client, "a")
    bide, _ = _auto_host(client, "b")

    job_id = _manual(client, aide).json()["id"]
    r = client.post("/api/v1/agent/next-job", auth=signed(atoken))
    assert r.status_code == 200 and r.json()["job"]["id"] == job_id
    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(atoken),
        json={
            "status": "succeeded",
            "exit_code": 0,
            "log": "ok",
            "boot_id": "boot-1",
            "will_reboot": True,
        },
    )
    assert r.status_code == 200, r.text
    assert _manual(client, bide).status_code == 409

    # Long past the return timeout, still no new boot: still held.
    job = db_session.get(Job, job_id)
    job.completed_at = datetime.now(timezone.utc) - timedelta(
        seconds=settings.campaign_return_timeout_seconds + 1
    )
    db_session.flush()
    assert _manual(client, bide).status_code == 409


def test_explicit_recovery_releases_unproven_hold(client, db_session, monkeypatch):
    _budget(monkeypatch, 1)
    aide, atoken = _auto_host(client, "a")
    bide, _ = _auto_host(client, "b")

    job_id = _manual(client, aide).json()["id"]
    r = client.post("/api/v1/agent/next-job", auth=signed(atoken))
    assert r.status_code == 200 and r.json()["job"]["id"] == job_id
    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(atoken),
        json={
            "status": "succeeded",
            "exit_code": 0,
            "log": "ok",
            "boot_id": "boot-1",
            "will_reboot": True,
        },
    )
    assert r.status_code == 200, r.text
    assert _manual(client, bide).status_code == 409

    # The operator abandons the guarantee by wiping the job history.
    r = client.delete(f"/api/v1/admin/hosts/{aide}/jobs", headers=ADMIN_HEADERS)
    assert r.status_code == 204
    assert _manual(client, bide).status_code == 201


def test_legacy_pending_auto_consumes_budget(client, db_session, monkeypatch):
    _budget(monkeypatch, 1)
    aide, _ = _auto_host(client, "a")
    bide, _ = _auto_host(client, "b")

    job_id = _manual(client, aide).json()["id"]
    _strip_pin(db_session, job_id)
    assert _manual(client, bide).status_code == 409  # policy fallback: auto


@pytest.mark.parametrize("policy", ["never", "prompt"])
def test_legacy_pending_non_auto_ignores_budget(client, db_session, monkeypatch, policy):
    _budget(monkeypatch, 1)
    aide, _ = create_host(client, hostname="a")
    r = client.patch(
        f"/api/v1/admin/hosts/{aide}",
        headers=ADMIN_HEADERS,
        json={"reboot_policy": policy},
    )
    assert r.status_code == 200, r.text
    bide, _ = _auto_host(client, "b")

    job_id = _manual(client, aide).json()["id"]
    _strip_pin(db_session, job_id)
    assert _manual(client, bide).status_code == 201  # policy fallback: no reboot
