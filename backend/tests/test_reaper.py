from datetime import datetime, timedelta, timezone

from app.models.models import Host, Job, WebhookDelivery
from app.scheduler import reap_stuck_jobs
from tests.conftest import create_host, webhook_row

TIMEOUT = 7200


def _running_job(db, host_id, *, started_age_seconds):
    job = Job(
        host_id=host_id,
        job_type="apt_upgrade",
        status="running",
        started_at=datetime.now(timezone.utc) - timedelta(seconds=started_age_seconds),
    )
    db.add(job)
    db.flush()
    return job


def _set_last_seen(db, host_id, *, age_seconds):
    host = db.get(Host, host_id)
    host.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    db.flush()


def test_reaper_fails_old_running_jobs(client, db_session):
    host_id, _ = create_host(client)
    _set_last_seen(db_session, host_id, age_seconds=30)  # host still reporting
    job = _running_job(db_session, host_id, started_age_seconds=TIMEOUT + 600)

    assert reap_stuck_jobs(db=db_session, timeout_seconds=TIMEOUT) == 1

    db_session.refresh(job)
    assert job.status == "failed"
    assert job.completed_at is not None
    assert job.result == {"reaped": True, "reason": "running timeout exceeded"}
    assert "scheduler reaper" in job.log
    assert job.failure_category == "timeout"
    assert "did not return" in job.failure_summary


def test_reaper_classifies_a_silent_host_as_agent_lost(client, db_session):
    host_id, _ = create_host(client)  # never reported -> last_seen_at is NULL
    job = _running_job(db_session, host_id, started_age_seconds=TIMEOUT + 600)

    assert reap_stuck_jobs(db=db_session, timeout_seconds=TIMEOUT) == 1

    db_session.refresh(job)
    assert job.failure_category == "agent_lost"

    # A host last heard from well past SILENT_AFTER is also agent_lost.
    host2, _ = create_host(client, hostname="stale")
    _set_last_seen(db_session, host2, age_seconds=3600)
    job2 = _running_job(db_session, host2, started_age_seconds=TIMEOUT + 600)
    reap_stuck_jobs(db=db_session, timeout_seconds=TIMEOUT)
    db_session.refresh(job2)
    assert job2.failure_category == "agent_lost"


def test_reaper_enqueues_job_failed_webhook(client, db_session):
    host_id, _ = create_host(client)
    _set_last_seen(db_session, host_id, age_seconds=30)
    webhook_row(db_session, events=("job.failed",), url="https://ko.test/h")
    job = _running_job(db_session, host_id, started_age_seconds=TIMEOUT + 600)

    assert reap_stuck_jobs(db=db_session, timeout_seconds=TIMEOUT) == 1

    rows = db_session.query(WebhookDelivery).all()
    assert len(rows) == 1
    data = rows[0].payload["data"]
    assert rows[0].event_type == "job.failed"
    assert data["job_id"] == str(job.id)
    assert data["reaped"] is True
    assert data["exit_code"] is None
    assert data["failure_category"] == "timeout"


def test_reaper_sets_the_columns_but_stages_nothing_when_webhooks_disabled(
    client, db_session, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "webhooks_enabled", False)
    host_id, _ = create_host(client)
    _set_last_seen(db_session, host_id, age_seconds=30)
    webhook_row(db_session, events=("job.failed",), url="https://ko.test/h")
    job = _running_job(db_session, host_id, started_age_seconds=TIMEOUT + 600)

    assert reap_stuck_jobs(db=db_session, timeout_seconds=TIMEOUT) == 1

    db_session.refresh(job)
    assert job.failure_category == "timeout"
    assert db_session.query(WebhookDelivery).count() == 0


def test_reaper_leaves_fresh_running_and_terminal_jobs(client, db_session):
    host_id, _ = create_host(client)
    fresh = _running_job(db_session, host_id, started_age_seconds=60)
    old_done = Job(
        host_id=host_id,
        job_type="apt_upgrade",
        status="succeeded",
        started_at=datetime.now(timezone.utc) - timedelta(hours=5),
        completed_at=datetime.now(timezone.utc) - timedelta(hours=5),
    )
    db_session.add(old_done)
    db_session.flush()

    assert reap_stuck_jobs(db=db_session, timeout_seconds=TIMEOUT) == 0

    db_session.refresh(fresh)
    assert fresh.status == "running"


def test_reaper_disabled_with_zero_timeout(client, db_session):
    host_id, _ = create_host(client)
    _running_job(db_session, host_id, started_age_seconds=86400)

    assert reap_stuck_jobs(db=db_session, timeout_seconds=0) == 0
