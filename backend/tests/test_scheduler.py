from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import settings
from app.models.models import Advisory, Job, Schedule, SchedulerState, WebhookDelivery
from app.scheduler import (
    ADVISORY_REFRESH_EVERY,
    HEARTBEAT_STATE_KEY,
    _advisory_due,
    _mark_advisory_done,
    _mark_heartbeat,
    run_advisory_refresh_if_due,
    sweep_webhook_deliveries,
    tick,
)
from tests.conftest import create_host, webhook_row


def _due_schedule(db, host_id, *, params=None):
    """A schedule whose window is already in the past."""
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    sched = Schedule(
        host_id=host_id,
        enabled=True,
        kind="weekly",
        weekday=0,
        hour=3,
        minute=0,
        timezone="UTC",
        params=params or {},
        next_run_at=past,
    )
    db.add(sched)
    db.flush()
    return sched


def test_tick_queues_a_job_and_advances(client, db_session):
    host_id, _ = create_host(client)
    sched = _due_schedule(db_session, host_id, params={"reboot": "auto"})

    now = datetime.now(timezone.utc)
    assert tick(now=now, db=db_session) == 1

    jobs = db_session.execute(
        select(Job).where(Job.host_id == host_id)
    ).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].requested_by == "scheduler"
    # excluded_packages is always injected for an apt_upgrade job (roadmap
    # item 3); empty here since no exclusion rule exists in this test.
    assert jobs[0].params == {"reboot": "auto", "excluded_packages": []}

    db_session.refresh(sched)
    assert sched.last_run_at == now
    assert sched.next_run_at > now


def test_tick_skips_when_host_has_active_job(client, db_session):
    host_id, _ = create_host(client)
    sched = _due_schedule(db_session, host_id)
    db_session.add(Job(host_id=host_id, job_type="apt_upgrade", status="pending"))
    db_session.flush()

    now = datetime.now(timezone.utc)
    assert tick(now=now, db=db_session) == 0

    jobs = db_session.execute(
        select(Job).where(Job.host_id == host_id)
    ).scalars().all()
    assert len(jobs) == 1  # only the pre-existing one
    db_session.refresh(sched)
    assert sched.next_run_at > now  # still advanced to the next window


def test_tick_ignores_disabled_and_future(client, db_session):
    host_id, _ = create_host(client)
    future = datetime.now(timezone.utc) + timedelta(days=1)
    db_session.add(
        Schedule(host_id=host_id, enabled=False, kind="weekly", weekday=0, hour=3,
                 minute=0, timezone="UTC", next_run_at=datetime.now(timezone.utc))
    )
    h2, _ = create_host(client, hostname="h2")
    db_session.add(
        Schedule(host_id=h2, enabled=True, kind="weekly", weekday=0, hour=3,
                 minute=0, timezone="UTC", next_run_at=future)
    )
    db_session.flush()

    assert tick(db=db_session) == 0


def test_heartbeat_is_upserted(db_session):
    first = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    _mark_heartbeat(db_session, first)
    stored = db_session.execute(
        select(SchedulerState.value).where(SchedulerState.key == HEARTBEAT_STATE_KEY)
    ).scalar_one()
    assert datetime.fromisoformat(stored) == first

    later = first + timedelta(minutes=1)
    _mark_heartbeat(db_session, later)
    stored = db_session.execute(
        select(SchedulerState.value).where(SchedulerState.key == HEARTBEAT_STATE_KEY)
    ).scalar_one()
    assert datetime.fromisoformat(stored) == later


# --- advisory feed refresh -------------------------------------------------

_FEED = (
    "[15 Aug 2026] DSA-5745-1 openssl - security update\n"
    "\t{CVE-2026-6119}\n"
    "\t[bookworm] - openssl 3.0.14-1~deb12u2\n"
)


def test_advisory_due_persisted_across_restarts(db_session):
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    assert _advisory_due(db_session, now) is True
    _mark_advisory_done(db_session, now)
    assert _advisory_due(db_session, now + timedelta(hours=1)) is False
    assert _advisory_due(db_session, now + ADVISORY_REFRESH_EVERY) is True


def test_advisory_refresh_runs_when_due(db_session, monkeypatch):
    monkeypatch.setattr(settings, "advisory_refresh_enabled", True)
    monkeypatch.setattr("app.advisories.debian.fetch", lambda url, **kw: _FEED)
    now = datetime.now(timezone.utc)

    run_advisory_refresh_if_due(now=now, db=db_session)

    assert db_session.get(Advisory, "DSA-5745-1") is not None
    assert _advisory_due(db_session, now) is False


def test_advisory_refresh_skips_when_recent(db_session, monkeypatch):
    monkeypatch.setattr(settings, "advisory_refresh_enabled", True)
    calls: list[str] = []
    monkeypatch.setattr(
        "app.advisories.debian.fetch", lambda url, **kw: calls.append(url) or _FEED
    )
    now = datetime.now(timezone.utc)
    _mark_advisory_done(db_session, now)

    run_advisory_refresh_if_due(now=now + timedelta(hours=1), db=db_session)

    assert calls == []


def test_advisory_refresh_keeps_state_key_on_fetch_failure(db_session, monkeypatch):
    monkeypatch.setattr(settings, "advisory_refresh_enabled", True)

    def boom(url, **kw):
        raise OSError("network down")

    monkeypatch.setattr("app.advisories.debian.fetch", boom)
    now = datetime.now(timezone.utc)

    run_advisory_refresh_if_due(now=now, db=db_session)

    assert _advisory_due(db_session, now) is True  # still due -> next tick retries
    assert db_session.execute(select(Advisory)).scalars().all() == []


def test_advisory_refresh_disabled_is_a_noop(db_session, monkeypatch):
    monkeypatch.setattr(settings, "advisory_refresh_enabled", False)

    def boom(url, **kw):
        raise AssertionError("must not fetch when the feature is disabled")

    monkeypatch.setattr("app.advisories.debian.fetch", boom)

    run_advisory_refresh_if_due(now=datetime.now(timezone.utc), db=db_session)


def _delivery(db, hook_id, *, status, completed_at):
    row = WebhookDelivery(
        webhook_id=hook_id,
        event_type="job.succeeded",
        payload={"event_type": "job.succeeded"},
        status=status,
        attempt_count=1,
        completed_at=completed_at,
    )
    db.add(row)
    db.flush()
    return row


def test_sweep_webhook_deliveries_removes_only_old_terminal_rows(db_session):
    hook = webhook_row(db_session)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=40)

    keep_pending = _delivery(db_session, hook.id, status="pending", completed_at=None)
    keep_recent = _delivery(db_session, hook.id, status="delivered", completed_at=now)
    drop_delivered = _delivery(db_session, hook.id, status="delivered", completed_at=old)
    drop_failed = _delivery(db_session, hook.id, status="failed", completed_at=old)

    deleted = sweep_webhook_deliveries(db_session, now, days=30)

    assert deleted == 2
    remaining = {r.id for r in db_session.query(WebhookDelivery).all()}
    assert remaining == {keep_pending.id, keep_recent.id}
    assert drop_delivered.id not in remaining
    assert drop_failed.id not in remaining


def test_sweep_webhook_deliveries_disabled_when_days_zero(db_session):
    hook = webhook_row(db_session)
    _delivery(
        db_session,
        hook.id,
        status="failed",
        completed_at=datetime.now(timezone.utc) - timedelta(days=999),
    )
    assert sweep_webhook_deliveries(db_session, datetime.now(timezone.utc), days=0) == 0
    assert db_session.query(WebhookDelivery).count() == 1
