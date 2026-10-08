"""Upgrade proof (6B, PR4): server-side success detection for agent_upgrade.

A running agent_upgrade succeeds only when a later authenticated contact
(poll or report) carries agent_version == params.target_version; an
unproven upgrade past CADENCE_UPGRADE_PROOF_TIMEOUT_SECONDS is failed by
the upgrade sweeper. No new status, no intermediate result, no process
memory. Real PostgreSQL throughout, including the thread+barrier races.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.models import Host, Job, WebhookDelivery
from app.scheduler import reap_stuck_jobs, sweep_unproven_agent_upgrades
from app.upgrade_proof import complete_agent_upgrade_if_proven
from tests.conftest import (
    ADMIN_HEADERS,
    create_host,
    report_payload,
    signed,
    webhook_row,
)

TARGET = "0.15.0"
PROOF_TIMEOUT = 600


def _create_upgrade(client, host_id: str, target: str = TARGET) -> str:
    r = client.post(
        f"/api/v1/admin/hosts/{host_id}/jobs",
        headers=ADMIN_HEADERS,
        json={"job_type": "agent_upgrade", "params": {"target_version": target}},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _poll(client, token: str, body: dict | None = None):
    if body is None:
        return client.post("/api/v1/agent/next-job", auth=signed(token))
    return client.post("/api/v1/agent/next-job", auth=signed(token), json=body)


def _running_upgrade(db, host_id, *, target=TARGET, started_age_seconds=10) -> Job:
    job = Job(
        host_id=host_id,
        job_type="agent_upgrade",
        status="running",
        params={"target_version": target},
        started_at=datetime.now(timezone.utc)
        - timedelta(seconds=started_age_seconds),
    )
    db.add(job)
    db.flush()
    return job


def _deliveries_for(db, job_id) -> list:
    return [
        d
        for d in db.query(WebhookDelivery).all()
        if (d.payload.get("data") or {}).get("job_id") == str(job_id)
    ]


def test_proof_via_poll_completes_the_upgrade(client, db_session):
    host_id, token = create_host(client)
    job_id = _create_upgrade(client, host_id)
    assert _poll(client, token, {}).status_code == 200  # claims it: running

    r = _poll(client, token, {"agent_version": TARGET})

    assert r.status_code == 200
    job = db_session.get(Job, job_id)
    assert job.status == "succeeded"
    assert job.completed_at is not None
    assert job.result == {"proven": True}
    assert "upgrade proof" in (job.log or "")
    assert db_session.get(Host, host_id).agent_version == TARGET


def test_proof_event_is_a_normal_job_succeeded(client, db_session):
    host_id, token = create_host(client)
    webhook_row(db_session, events=("job.succeeded",))
    job_id = _create_upgrade(client, host_id)
    _poll(client, token, {})

    _poll(client, token, {"agent_version": TARGET})

    rows = _deliveries_for(db_session, job_id)
    assert len(rows) == 1
    assert rows[0].event_type == "job.succeeded"
    data = rows[0].payload["data"]
    assert data["status"] == "succeeded"
    assert data["exit_code"] == 0
    assert data["job_type"] == "agent_upgrade"


def test_proof_via_report_completes_and_persists_the_report(client, db_session):
    host_id, token = create_host(client)
    job_id = _create_upgrade(client, host_id)
    _poll(client, token, {})

    r = client.post(
        "/api/v1/reports",
        auth=signed(token),
        json=report_payload(agent_version=TARGET),
    )

    assert r.status_code == 200, r.text
    assert r.json()["installed_package_count"] == 0  # report itself persisted
    assert db_session.get(Job, job_id).status == "succeeded"


def test_health_check_poll_proves_before_admission(client, db_session):
    host_id, token = create_host(client)
    job_id = _create_upgrade(client, host_id)
    _poll(client, token, {})

    r = client.post(
        "/api/v1/agent/health-check-job",
        auth=signed(token),
        json={"agent_version": TARGET},
    )

    assert r.status_code == 200
    assert db_session.get(Job, job_id).status == "succeeded"
    # The proven upgrade freed the one-active-job guard: a health_check job
    # could be created and handed back in the same contact.
    assert (r.json()["job"] or {}).get("job_type") == "health_check"


def test_absent_version_leaves_the_upgrade_running(client, db_session):
    host_id, token = create_host(client)
    job_id = _create_upgrade(client, host_id)
    _poll(client, token, {})

    assert _poll(client, token, {}).status_code == 200
    assert _poll(client, token).status_code == 200  # no body at all
    r = client.post(
        "/api/v1/reports", auth=signed(token), json=report_payload()
    )
    assert r.status_code == 200, r.text

    assert db_session.get(Job, job_id).status == "running"


def test_wrong_version_leaves_the_upgrade_running(client, db_session):
    host_id, token = create_host(client)
    job_id = _create_upgrade(client, host_id)
    _poll(client, token, {})

    _poll(client, token, {"agent_version": "0.14.0"})
    client.post(
        "/api/v1/reports",
        auth=signed(token),
        json=report_payload(agent_version="0.14.0"),
    )

    assert db_session.get(Job, job_id).status == "running"


def test_double_proof_emits_a_single_event(client, db_session):
    host_id, token = create_host(client)
    webhook_row(db_session, events=("job.succeeded",))
    job_id = _create_upgrade(client, host_id)
    _poll(client, token, {})

    _poll(client, token, {"agent_version": TARGET})
    client.post(
        "/api/v1/reports",
        auth=signed(token),
        json=report_payload(agent_version=TARGET),
    )

    assert db_session.get(Job, job_id).status == "succeeded"
    assert len(_deliveries_for(db_session, job_id)) == 1


def test_proof_ignores_other_job_types(client, db_session):
    host_id, token = create_host(client)
    job = Job(
        host_id=host_id,
        job_type="apt_upgrade",
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(job)
    db_session.flush()

    _poll(client, token, {"agent_version": TARGET})

    db_session.refresh(job)
    assert job.status == "running"


def test_proof_after_expunge_needs_no_process_memory(client, db_session):
    host_id, token = create_host(client)
    job_id = _create_upgrade(client, host_id)
    _poll(client, token, {})
    db_session.commit()
    db_session.expunge_all()  # whatever the process knew is gone

    _poll(client, token, {"agent_version": TARGET})

    assert db_session.get(Job, job_id).status == "succeeded"


def test_sweeper_fails_an_old_unproven_upgrade(client, db_session):
    host_id, _ = create_host(client)
    webhook_row(db_session, events=("job.failed",))
    job = _running_upgrade(db_session, host_id, started_age_seconds=PROOF_TIMEOUT + 60)

    assert sweep_unproven_agent_upgrades(db=db_session, timeout_seconds=PROOF_TIMEOUT) == 1

    db_session.refresh(job)
    assert job.status == "failed"
    assert job.completed_at is not None
    assert job.failure_category == "upgrade_proof_timeout"
    assert "never matched target_version" in job.failure_summary
    assert job.result == {"reaped": True, "reason": "upgrade proof timeout exceeded"}
    assert "upgrade sweeper" in job.log
    rows = _deliveries_for(db_session, job.id)
    assert len(rows) == 1
    assert rows[0].event_type == "job.failed"
    assert rows[0].payload["data"]["reaped"] is True
    assert rows[0].payload["data"]["failure_category"] == "upgrade_proof_timeout"


def test_sweeper_leaves_a_fresh_upgrade_running(client, db_session):
    host_id, _ = create_host(client)
    job = _running_upgrade(db_session, host_id, started_age_seconds=60)

    assert sweep_unproven_agent_upgrades(db=db_session, timeout_seconds=PROOF_TIMEOUT) == 0

    db_session.refresh(job)
    assert job.status == "running"


def test_sweeper_ignores_other_job_types(client, db_session):
    host_id, _ = create_host(client)
    job = Job(
        host_id=host_id,
        job_type="apt_upgrade",
        status="running",
        started_at=datetime.now(timezone.utc) - timedelta(seconds=PROOF_TIMEOUT + 60),
    )
    db_session.add(job)
    db_session.flush()

    assert sweep_unproven_agent_upgrades(db=db_session, timeout_seconds=PROOF_TIMEOUT) == 0

    db_session.refresh(job)
    assert job.status == "running"


def test_sweeper_disabled_with_zero_timeout(client, db_session):
    host_id, _ = create_host(client)
    _running_upgrade(db_session, host_id, started_age_seconds=86400)

    assert sweep_unproven_agent_upgrades(db=db_session, timeout_seconds=0) == 0


def test_sweeper_reads_its_timeout_from_settings(client, db_session, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "upgrade_proof_timeout_seconds", PROOF_TIMEOUT)
    host_id, _ = create_host(client)
    job = _running_upgrade(db_session, host_id, started_age_seconds=PROOF_TIMEOUT + 60)

    assert sweep_unproven_agent_upgrades(db=db_session) == 1

    db_session.refresh(job)
    assert job.status == "failed"


def test_generic_reaper_stays_the_backstop_when_the_sweeper_is_disabled(
    client, db_session
):
    host_id, _ = create_host(client)
    host = db_session.get(Host, host_id)
    host.last_seen_at = datetime.now(timezone.utc)  # still reporting
    job = _running_upgrade(db_session, host_id, started_age_seconds=8000)

    assert sweep_unproven_agent_upgrades(db=db_session, timeout_seconds=0) == 0
    assert reap_stuck_jobs(db=db_session, timeout_seconds=7200) == 1

    db_session.refresh(job)
    assert job.status == "failed"
    assert job.failure_category == "timeout"


def test_proof_timeout_config_defaults_and_guards_coherence(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("CADENCE_ADMIN_KEY", "a-perfectly-fine-admin-key")
    monkeypatch.delenv("CADENCE_UPGRADE_PROOF_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("CADENCE_JOB_RUNNING_TIMEOUT_SECONDS", raising=False)
    assert Settings().upgrade_proof_timeout_seconds == 600

    monkeypatch.setenv("CADENCE_UPGRADE_PROOF_TIMEOUT_SECONDS", "0")
    assert Settings().upgrade_proof_timeout_seconds == 0

    monkeypatch.setenv("CADENCE_UPGRADE_PROOF_TIMEOUT_SECONDS", "9000")
    monkeypatch.setenv("CADENCE_JOB_RUNNING_TIMEOUT_SECONDS", "7200")
    with pytest.raises(RuntimeError, match="must not exceed"):
        Settings()


def test_proof_timeout_config_enforces_the_distributed_floor(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("CADENCE_ADMIN_KEY", "a-perfectly-fine-admin-key")
    monkeypatch.delenv("CADENCE_JOB_RUNNING_TIMEOUT_SECONDS", raising=False)

    # The agent's local pre-commit deadline (300s) plus margin: anything
    # non-zero below 420s could fail the job while the agent is still
    # legitimately verifying.
    for too_short in ("1", "30", "300", "419"):
        monkeypatch.setenv("CADENCE_UPGRADE_PROOF_TIMEOUT_SECONDS", too_short)
        with pytest.raises(RuntimeError, match="at least 420"):
            Settings()

    for ok in ("420", "421", "600"):
        monkeypatch.setenv("CADENCE_UPGRADE_PROOF_TIMEOUT_SECONDS", ok)
        assert Settings().upgrade_proof_timeout_seconds == int(ok)

    # The floor and the generic relation compose: 420 is fine unless the
    # generic timeout is even shorter.
    monkeypatch.setenv("CADENCE_UPGRADE_PROOF_TIMEOUT_SECONDS", "420")
    monkeypatch.setenv("CADENCE_JOB_RUNNING_TIMEOUT_SECONDS", "300")
    with pytest.raises(RuntimeError, match="must not exceed"):
        Settings()


def test_proof_timeout_zero_defers_the_floor_to_generic(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("CADENCE_ADMIN_KEY", "a-perfectly-fine-admin-key")
    monkeypatch.setenv("CADENCE_UPGRADE_PROOF_TIMEOUT_SECONDS", "0")

    # The generic reaper is the effective upgrade terminalizer: it must
    # respect the same floor, or the server could fail the job while the
    # agent is still legitimately pre-commit.
    for too_short in ("1", "120", "300", "419"):
        monkeypatch.setenv("CADENCE_JOB_RUNNING_TIMEOUT_SECONDS", too_short)
        with pytest.raises(RuntimeError, match="JOB_RUNNING_TIMEOUT"):
            Settings()

    for ok in ("420", "7200"):
        monkeypatch.setenv("CADENCE_JOB_RUNNING_TIMEOUT_SECONDS", ok)
        s = Settings()
        assert s.upgrade_proof_timeout_seconds == 0
        assert s.job_running_timeout_seconds == int(ok)

    # Both disabled: nothing server-side can terminalize; the agent still
    # self-aborts and submits failed at 300s.
    monkeypatch.setenv("CADENCE_JOB_RUNNING_TIMEOUT_SECONDS", "0")
    assert Settings().job_running_timeout_seconds == 0


def _concurrent_setup(maker, *, started_age_seconds):
    """Commit one running upgrade + webhooks outside the rolled-back txn."""
    from tests.test_robustness_t0 import _t0_host

    setup = maker()
    try:
        host = _t0_host(setup, f"vm-proof-{uuid.uuid4().hex[:6]}")
        host_id = host.id
        job = Job(
            host_id=host_id,
            job_type="agent_upgrade",
            status="running",
            params={"target_version": TARGET},
            started_at=datetime.now(timezone.utc)
            - timedelta(seconds=started_age_seconds),
        )
        setup.add(job)
        setup.flush()
        job_id = job.id
        hook_id = webhook_row(
            setup, events=("job.succeeded", "job.failed")
        ).id
        setup.commit()
    finally:
        setup.close()
    return host_id, job_id, hook_id


def test_concurrent_proofs_elect_a_single_winner(db_session):
    from tests.test_robustness_t0 import _maker, _run_pair, _t0_cleanup

    maker = _maker()
    host_id, job_id, hook_id = _concurrent_setup(maker, started_age_seconds=10)
    try:

        def _prove(db):
            host = db.get(Host, host_id)
            won = complete_agent_upgrade_if_proven(
                db, host, TARGET, datetime.now(timezone.utc)
            )
            db.commit()
            return won is not None

        outcomes = _run_pair(_prove)
        assert sorted(outcomes) == [False, True]

        check = maker()
        try:
            assert check.get(Job, job_id).status == "succeeded"
            assert len(_deliveries_for(check, job_id)) == 1
        finally:
            check.close()
    finally:
        _t0_cleanup(host_ids=[host_id], hook_ids=[hook_id])


def test_proof_vs_sweeper_race_has_a_single_terminal_outcome(db_session):
    from tests.test_robustness_t0 import _maker, _t0_cleanup

    maker = _maker()
    host_id, job_id, hook_id = _concurrent_setup(
        maker, started_age_seconds=PROOF_TIMEOUT + 60
    )
    try:

        def _prove(db):
            host = db.get(Host, host_id)
            won = complete_agent_upgrade_if_proven(
                db, host, TARGET, datetime.now(timezone.utc)
            )
            db.commit()
            return "proven" if won else "lost"

        def _sweep(db):
            swept = sweep_unproven_agent_upgrades(
                db=db, timeout_seconds=PROOF_TIMEOUT
            )
            return "swept" if swept else "lost"

        barrier = threading.Barrier(2)
        results: dict[str, object] = {}

        def _one(tag, fn):
            db = maker()
            try:
                barrier.wait(timeout=30)
                results[tag] = fn(db)
            except Exception as exc:  # noqa: BLE001 -- reported, not hidden
                db.rollback()
                results[tag] = exc
            finally:
                db.close()

        threads = [
            threading.Thread(target=_one, args=("prove", _prove), daemon=True),
            threading.Thread(target=_one, args=("sweep", _sweep), daemon=True),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert sorted(str(v) for v in results.values()) == ["lost", "proven"] or sorted(
            str(v) for v in results.values()
        ) == ["lost", "swept"], results

        check = maker()
        try:
            stored = check.get(Job, job_id)
            assert stored.status in ("succeeded", "failed")
            assert len(_deliveries_for(check, job_id)) == 1
        finally:
            check.close()
    finally:
        _t0_cleanup(host_ids=[host_id], hook_ids=[hook_id])


def test_old_poll_shapes_still_claim_unchanged(client, db_session):
    host_id, token = create_host(client)
    job_id = _create_upgrade(client, host_id)

    r = _poll(client, token, {})
    assert r.status_code == 200
    assert r.json()["job"]["id"] == job_id
    r = _poll(client, token)  # no body at all
    assert r.status_code == 200
    assert r.json()["job"] is None  # already running: nothing more to claim
    assert db_session.get(Job, job_id).status == "running"

    r = _poll(client, token, {"agent_version": "0.14.0", "boot_id": "boot-1"})
    assert r.status_code == 200
    host = db_session.get(Host, host_id)
    assert host.agent_version == "0.14.0"
    assert host.current_boot_id == "boot-1"
