from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.models import AgentToken, AuditLog, Job, Report
from app.scheduler import (
    RETENTION_EVERY,
    _mark_retention_done,
    _retention_due,
    retention_sweep,
)
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


def _token(db, host_id, *, token_hash, revoked_age_days=None, expires_age_days=None):
    db.add(
        AgentToken(
            host_id=host_id,
            token_hash=token_hash,
            revoked_at=None
            if revoked_age_days is None
            else NOW - timedelta(days=revoked_age_days),
            expires_at=None
            if expires_age_days is None
            else NOW - timedelta(days=expires_age_days),
        )
    )


def test_sweep_deletes_old_reports_only(client, db_session):
    host_id, _ = create_host(client)
    _report(db_session, host_id, age_days=200)
    _report(db_session, host_id, age_days=100)
    _report(db_session, host_id, age_days=10)
    db_session.flush()

    reports, jobs, audit, tokens = retention_sweep(
        db_session, NOW, reports_days=90, jobs_days=90, audit_days=0, tokens_days=0
    )
    assert (reports, jobs, audit, tokens) == (2, 0, 0, 0)

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

    _, jobs, _, _ = retention_sweep(
        db_session, NOW, reports_days=0, jobs_days=90, audit_days=0, tokens_days=0
    )
    assert jobs == 2  # the two old terminal jobs

    remaining = {
        j.status
        for j in db_session.execute(
            select(Job).where(Job.host_id == host_id)
        ).scalars()
    }
    assert remaining == {"succeeded", "pending", "running"}


def test_sweep_keeps_old_jobs_while_their_campaign_is_live(client, db_session):
    from app.models.models import Campaign

    host_id, _ = create_host(client)

    def _campaign(status):
        c = Campaign(
            name="c",
            stages=[1],
            max_concurrency=1,
            max_failures=0,
            observation_window_seconds=0,
            status=status,
        )
        db_session.add(c)
        db_session.flush()
        return c

    live = _campaign("running")
    finished = _campaign("completed")
    old = dict(
        job_type="apt_upgrade",
        status="succeeded",
        completed_at=NOW - timedelta(days=200),
    )
    j_live = Job(host_id=host_id, campaign_id=live.id, **old)
    j_finished = Job(host_id=host_id, campaign_id=finished.id, **old)
    j_plain = Job(host_id=host_id, **old)
    db_session.add_all([j_live, j_finished, j_plain])
    db_session.flush()

    _, jobs, _, _ = retention_sweep(
        db_session, NOW, reports_days=0, jobs_days=90, audit_days=0, tokens_days=0
    )
    assert jobs == 2  # the finished-campaign job and the plain job
    remaining = set(
        db_session.execute(
            select(Job.id).where(Job.host_id == host_id)
        ).scalars()
    )
    assert remaining == {j_live.id}


def test_sweep_disabled_with_zero(client, db_session):
    host_id, _ = create_host(client)
    _report(db_session, host_id, age_days=999)
    _job(db_session, host_id, status="succeeded", completed_age_days=999)
    db_session.flush()

    assert retention_sweep(
        db_session, NOW, reports_days=0, jobs_days=0, audit_days=0, tokens_days=0
    ) == (0, 0, 0, 0)


def test_sweep_deletes_old_audit_rows_only(db_session):
    db_session.add(AuditLog(at=NOW - timedelta(days=400), action="host.create"))
    db_session.add(AuditLog(at=NOW - timedelta(days=200), action="host.delete"))
    db_session.flush()

    reports, jobs, audit, tokens = retention_sweep(
        db_session, NOW, reports_days=0, jobs_days=0, audit_days=365, tokens_days=0
    )
    assert (reports, jobs, audit, tokens) == (0, 0, 1, 0)

    left = db_session.execute(select(AuditLog.action)).scalars().all()
    assert left == ["host.delete"]


def _token_hashes(db, host_id):
    return {
        t.token_hash
        for t in db.execute(
            select(AgentToken).where(AgentToken.host_id == host_id)
        ).scalars()
    }


def test_sweep_deletes_stale_agent_tokens_only(client, db_session):
    # create_host already issued one (active) token for this host.
    host_id, _ = create_host(client)
    _token(db_session, host_id, token_hash="rt-revoked-old", revoked_age_days=200)
    _token(db_session, host_id, token_hash="rt-expired-old", expires_age_days=200)
    _token(db_session, host_id, token_hash="rt-revoked-recent", revoked_age_days=5)
    _token(db_session, host_id, token_hash="rt-active")  # no revoke, no expiry
    db_session.flush()
    before = _token_hashes(db_session, host_id)

    reports, jobs, audit, tokens = retention_sweep(
        db_session, NOW, reports_days=0, jobs_days=0, audit_days=0, tokens_days=90
    )
    assert (reports, jobs, audit, tokens) == (0, 0, 0, 2)

    after = _token_hashes(db_session, host_id)
    assert {"rt-revoked-old", "rt-expired-old"}.isdisjoint(after)
    assert {"rt-revoked-recent", "rt-active"} <= after
    assert after == before - {"rt-revoked-old", "rt-expired-old"}


def test_sweep_keeps_all_tokens_when_disabled(client, db_session):
    host_id, _ = create_host(client)
    _token(db_session, host_id, token_hash="rt-revoked-ancient", revoked_age_days=999)
    db_session.flush()

    _, _, _, tokens = retention_sweep(
        db_session, NOW, reports_days=0, jobs_days=0, audit_days=0, tokens_days=0
    )
    assert tokens == 0
    assert "rt-revoked-ancient" in _token_hashes(db_session, host_id)


def test_retention_due_is_persisted_across_restarts(client, db_session):
    # Nothing recorded yet -> due.
    assert _retention_due(db_session, NOW) is True

    _mark_retention_done(db_session, NOW)

    # A fresh scheduler process (no in-memory state) still sees it as not due.
    assert _retention_due(db_session, NOW + timedelta(hours=1)) is False
    assert _retention_due(db_session, NOW + RETENTION_EVERY) is True
