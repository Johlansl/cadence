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

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.schedule_timing import next_run_at
from app.db.base import SessionLocal
from app.models.models import Job, Report, Schedule

log = logging.getLogger("cadence.scheduler")

TICK_SECONDS = 60
RETENTION_EVERY = timedelta(hours=24)
_stop = False
_last_retention: datetime | None = None


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
                log.info("schedule %s: window open -> job queued for host %s", sched.id, sched.host_id)
            else:
                log.info(
                    "schedule %s: skipped, host %s has an active job -> next window",
                    sched.id,
                    sched.host_id,
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


def run_retention_if_due(now: datetime | None = None) -> None:
    global _last_retention
    now = now or datetime.now(timezone.utc)
    if _last_retention is not None and now - _last_retention < RETENTION_EVERY:
        return
    _last_retention = now
    if settings.reports_retention_days == 0 and settings.jobs_retention_days == 0:
        return
    with SessionLocal() as db:
        reports, jobs = retention_sweep(
            db,
            now,
            reports_days=settings.reports_retention_days,
            jobs_days=settings.jobs_retention_days,
        )
    log.info(
        "retention sweep: %d reports, %d jobs deleted (keep reports=%dd jobs=%dd)",
        reports,
        jobs,
        settings.reports_retention_days,
        settings.jobs_retention_days,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    log.info("cadence scheduler started (tick=%ss)", TICK_SECONDS)
    while not _stop:
        try:
            tick()
            run_retention_if_due()
        except Exception:  # noqa: BLE001 -- keep the loop alive
            log.exception("scheduler tick failed")
        for _ in range(TICK_SECONDS):
            if _stop:
                break
            time.sleep(1)
    log.info("cadence scheduler stopped")


if __name__ == "__main__":
    main()
