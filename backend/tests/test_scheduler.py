from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.models import Job, Schedule, SchedulerState
from app.scheduler import HEARTBEAT_STATE_KEY, _mark_heartbeat, tick
from tests.conftest import ADMIN_HEADERS, create_host


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
    assert jobs[0].params == {"reboot": "auto"}

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
