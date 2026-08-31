"""Cadence scheduler.

A standalone loop (docker-compose `scheduler` service, same image as the
backend, `python -m app.scheduler`). Every minute it turns due `schedules`
rows into `jobs` rows. It only writes to the database -- agents still pull
jobs, so the outbound-only model is untouched.

If a host already has an active job when its window opens, the run is skipped
and the schedule advances to the next window (no catch-up).
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import configure_logging
from app.core.schedule_timing import next_run_at
from app.db.base import SessionLocal
from app.models.models import Job, Report, Schedule, SchedulerState

log = logging.getLogger("cadence.scheduler")


def _f(**fields: object) -> dict:
    """Wrap structured fields for the logfmt formatter."""
    return {"fields": fields}

TICK_SECONDS = 60
RETENTION_EVERY = timedelta(hours=24)
RETENTION_STATE_KEY = "last_retention_at"
HEARTBEAT_STATE_KEY = "last_tick_at"
_stop = False


def _request_stop(*_: object) -> None:
    global _stop
    _stop = True


def tick(now: datetime | None = None, db: Session | None = None) -> int:
    """Process every due schedule once. Returns the number of jobs queued.
    Pass `db` to run inside an existing session (tests); otherwise a session
    is opened and closed here."""
    now = now or datetime.now(timezone.utc)
    own_session = db is None
    db = db or SessionLocal()
    queued = 0
    try:
        due = (
            db.execute(
                select(Schedule)
                .where(
                    Schedule.enabled.is_(True),
                    Schedule.next_run_at.is_not(None),
                    Schedule.next_run_at <= now,
                )
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        for sched in due:
            has_active_job = db.execute(
                select(Job.id)
                .where(Job.host_id == sched.host_id, Job.status.in_(("pending", "running")))
                .limit(1)
            ).first()
            if has_active_job is None:
                db.add(
                    Job(
                        host_id=sched.host_id,
                        job_type="apt_upgrade",
                        params=dict(sched.params),
                        requested_by="scheduler",
                    )
                )
                sched.last_run_at = now
                queued += 1
                log.info(
                    "schedule window open, job queued",
                    extra=_f(schedule_id=sched.id, host_id=sched.host_id),
                )
            else:
                log.info(
                    "schedule window skipped, host busy",
                    extra=_f(schedule_id=sched.id, host_id=sched.host_id),
                )
            sched.next_run_at = next_run_at(
                kind=sched.kind,
                day_of_month=sched.day_of_month,
                weekday=sched.weekday,
                hour=sched.hour,
                minute=sched.minute,
                timezone=sched.timezone,
                after=now,
            )
            sched.updated_at = now
        db.commit()
    finally:
        if own_session:
            db.close()
    return queued


def reap_stuck_jobs(
    now: datetime | None = None,
    db: Session | None = None,
    *,
    timeout_seconds: int | None = None,
) -> int:
    """Fail jobs stuck in 'running' past the timeout. An agent that claims a
    job and never posts a result would otherwise block every future job for
    that host (create_job and tick both refuse a host with an active job).
    0 = disabled. Returns the number of jobs reaped."""
    if timeout_seconds is None:
        timeout_seconds = settings.job_running_timeout_seconds
    if timeout_seconds <= 0:
        return 0
    now = now or datetime.now(timezone.utc)
    own_session = db is None
    db = db or SessionLocal()
    cutoff = now - timedelta(seconds=timeout_seconds)
    try:
        result = db.execute(
            update(Job)
            .where(
                Job.status == "running",
                Job.started_at.is_not(None),
                Job.started_at < cutoff,
            )
            .values(
                status="failed",
                completed_at=now,
                result={"reaped": True, "reason": "running timeout exceeded"},
                log=func.concat(
                    func.coalesce(Job.log, ""),
                    f"\n[cadence] no result after {timeout_seconds}s; "
                    "marked failed by the scheduler reaper",
                ),
            )
        )
        reaped = result.rowcount
        db.commit()
    finally:
        if own_session:
            db.close()
    if reaped:
        log.warning(
            "reaped stuck running jobs",
            extra=_f(count=reaped, timeout_seconds=timeout_seconds),
        )
    return reaped


def retention_sweep(
    db: Session, now: datetime, *, reports_days: int, jobs_days: int
) -> tuple[int, int]:
    """Delete old append-only rows. 0 days = keep forever. Terminal jobs only
    (pending/running are never removed here). Returns (reports, jobs) deleted."""
    reports_deleted = 0
    jobs_deleted = 0
    if reports_days > 0:
        cutoff = now - timedelta(days=reports_days)
        reports_deleted = db.execute(
            delete(Report).where(Report.received_at < cutoff)
        ).rowcount
    if jobs_days > 0:
        cutoff = now - timedelta(days=jobs_days)
        jobs_deleted = db.execute(
            delete(Job).where(
                Job.status.in_(("succeeded", "failed")),
                Job.completed_at.is_not(None),
                Job.completed_at < cutoff,
            )
        ).rowcount
    db.commit()
    return reports_deleted, jobs_deleted


def _retention_due(db: Session, now: datetime) -> bool:
    """True when a sweep hasn't run within RETENTION_EVERY. The last-run time
    is persisted in scheduler_state so a process restart doesn't re-trigger."""
    last = db.execute(
        select(SchedulerState.value).where(SchedulerState.key == RETENTION_STATE_KEY)
    ).scalar_one_or_none()
    if last is None:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return now - last_dt >= RETENTION_EVERY


def _mark_retention_done(db: Session, now: datetime) -> None:
    db.execute(
        pg_insert(SchedulerState)
        .values(key=RETENTION_STATE_KEY, value=now.isoformat(), updated_at=now)
        .on_conflict_do_update(
            index_elements=["key"], set_={"value": now.isoformat(), "updated_at": now}
        )
    )
    db.commit()


def run_retention_if_due(now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    if settings.reports_retention_days == 0 and settings.jobs_retention_days == 0:
        return
    with SessionLocal() as db:
        if not _retention_due(db, now):
            return
        reports, jobs = retention_sweep(
            db,
            now,
            reports_days=settings.reports_retention_days,
            jobs_days=settings.jobs_retention_days,
        )
        _mark_retention_done(db, now)
    log.info(
        "retention sweep done",
        extra=_f(
            reports_deleted=reports,
            jobs_deleted=jobs,
            keep_reports_days=settings.reports_retention_days,
            keep_jobs_days=settings.jobs_retention_days,
        ),
    )


def _mark_heartbeat(db: Session, now: datetime) -> None:
    db.execute(
        pg_insert(SchedulerState)
        .values(key=HEARTBEAT_STATE_KEY, value=now.isoformat(), updated_at=now)
        .on_conflict_do_update(
            index_elements=["key"], set_={"value": now.isoformat(), "updated_at": now}
        )
    )
    db.commit()


def record_heartbeat(now: datetime | None = None) -> None:
    """Persist 'the loop finished a full pass at this time'. The scheduler
    service healthcheck (app.scheduler_healthcheck) fails once this goes
    stale, catching a wedged loop that a bare PID check would miss."""
    now = now or datetime.now(timezone.utc)
    with SessionLocal() as db:
        _mark_heartbeat(db, now)


def main() -> None:
    configure_logging()
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    log.info("scheduler started", extra=_f(tick_seconds=TICK_SECONDS))
    while not _stop:
        try:
            reap_stuck_jobs()
            tick()
            run_retention_if_due()
            record_heartbeat()
        except Exception:  # noqa: BLE001 -- keep the loop alive
            log.exception("scheduler tick failed")
        for _ in range(TICK_SECONDS):
            if _stop:
                break
            time.sleep(1)
    log.info("scheduler stopped")


if __name__ == "__main__":
    main()
