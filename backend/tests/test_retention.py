from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.models import Job, Report
from app.scheduler import retention_sweep
from tests.conftest import create_host

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _report(db, host_id, age_days):
    db.add(
        Report(
            host_id=host_id,
            received_at=NOW - timedelta(days=age_days),
            raw_payload={},
        )
    )


def _job(db, host_id, *, status, completed_age_days=None, created_age_days=0):
    db.add(
        Job(
            host_id=host_id,
            job_type="apt_upgrade",
            status=status,
            created_at=NOW - timedelta(days=created_age_days),
            completed_at=None
            if completed_age_days is None
            else NOW - timedelta(days=completed_age_days),
        )
    )


def test_sweep_deletes_old_reports_only(client, db_session):
    host_id, _ = create_host(client)
    _report(db_session, host_id, age_days=200)
    _report(db_session, host_id, age_days=100)
    _report(db_session, host_id, age_days=10)
    db_session.flush()

    reports, jobs = retention_sweep(db_session, NOW, reports_days=90, jobs_days=90)
    assert (reports, jobs) == (2, 0)

    left = db_session.execute(
        select(Report).where(Report.host_id == host_id)
    ).scalars().all()
    assert len(left) == 1


def test_sweep_keeps_pending_and_recent_jobs(client, db_session):
    host_id, _ = create_host(client)
    _job(db_session, host_id, status="succeeded", completed_age_days=200)
    _job(db_session, host_id, status="failed", completed_age_days=200)
    _job(db_session, host_id, status="succeeded", completed_age_days=5)
    _job(db_session, host_id, status="pending", created_age_days=300)
    _job(db_session, host_id, status="running", created_age_days=300)
    db_session.flush()

    _, jobs = retention_sweep(db_session, NOW, reports_days=0, jobs_days=90)
    assert jobs == 2  # the two old terminal jobs

    remaining = {
        j.status
        for j in db_session.execute(
            select(Job).where(Job.host_id == host_id)
        ).scalars()
    }
    assert remaining == {"succeeded", "pending", "running"}


def test_sweep_disabled_with_zero(client, db_session):
    host_id, _ = create_host(client)
    _report(db_session, host_id, age_days=999)
    _job(db_session, host_id, status="succeeded", completed_age_days=999)
    db_session.flush()

    assert retention_sweep(db_session, NOW, reports_days=0, jobs_days=0) == (0, 0)
