from datetime import datetime, timedelta, timezone

from app.models.models import Job
from app.scheduler import reap_stuck_jobs
from tests.conftest import create_host

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


def test_reaper_fails_old_running_jobs(client, db_session):
    host_id, _ = create_host(client)
    job = _running_job(db_session, host_id, started_age_seconds=TIMEOUT + 600)

    assert reap_stuck_jobs(db=db_session, timeout_seconds=TIMEOUT) == 1

    db_session.refresh(job)
    assert job.status == "failed"
    assert job.completed_at is not None
    assert job.result == {"reaped": True, "reason": "running timeout exceeded"}
    assert "scheduler reaper" in job.log


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
