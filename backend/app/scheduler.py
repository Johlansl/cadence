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

from sqlalchemy import case, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.advisories import debian
from app.advisories.sync import refresh_advisories
from app.core.config import settings
from app.core.logging import configure_logging
from app.core.schedule_timing import next_run_at
from app.core.staleness import SILENT_AFTER
from app.db.base import SessionLocal
from app.exclusions import known_held_for_host, resolve_for_job
from app.models.models import (
    AgentToken,
    AuditLog,
    Host,
    HostPackage,
    Job,
    Package,
    Report,
    Schedule,
    SchedulerState,
    WebhookDelivery,
)
from app.webhooks.delivery import dispatch_pending_deliveries
from app.webhooks.events import on_job_reaped
from app.webhooks.offline import scan_offline_hosts

log = logging.getLogger("cadence.scheduler")


def _f(**fields: object) -> dict:
    """Wrap structured fields for the logfmt formatter."""
    return {"fields": fields}

TICK_SECONDS = 60
RETENTION_EVERY = timedelta(hours=24)
RETENTION_STATE_KEY = "last_retention_at"
HEARTBEAT_STATE_KEY = "last_tick_at"
ADVISORY_REFRESH_EVERY = timedelta(hours=6)
ADVISORY_STATE_KEY = "last_advisory_refresh_at"
PACKAGES_GC_EVERY = timedelta(days=7)
PACKAGES_GC_STATE_KEY = "last_packages_gc_at"
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
                params = dict(sched.params)
                params["excluded_packages"] = resolve_for_job(db, sched.host_id)
                params["known_held_packages"] = known_held_for_host(db, sched.host_id)
                db.add(
                    Job(
                        host_id=sched.host_id,
                        job_type="apt_upgrade",
                        params=params,
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
    # A reaped job has no agent result to classify. Split the two no-result
    # cases on how recently the host was last heard from: still reporting means
    # the job itself overran ("timeout"); silent means the agent is gone
    # ("agent_lost"). A NULL last_seen_at (host never reported) counts as silent.
    seen_recently = (
        select(Host.last_seen_at).where(Host.id == Job.host_id).scalar_subquery()
    ) >= (now - SILENT_AFTER)
    try:
        rows = db.execute(
            update(Job)
            .where(
                Job.status == "running",
                Job.started_at.is_not(None),
                Job.started_at < cutoff,
            )
            .values(
                status="failed",
                completed_at=now,
                failure_category=case((seen_recently, "timeout"), else_="agent_lost"),
                failure_summary=case(
                    (
                        seen_recently,
                        f"no result after {timeout_seconds}s; host still reporting, "
                        "the job did not return",
                    ),
                    else_=f"no result after {timeout_seconds}s; host silent, "
                    "the agent is presumed lost",
                ),
                result={"reaped": True, "reason": "running timeout exceeded"},
                log=func.concat(
                    func.coalesce(Job.log, ""),
                    f"\n[cadence] no result after {timeout_seconds}s; "
                    "marked failed by the scheduler reaper",
                ),
            )
            .returning(
                Job.id,
                Job.host_id,
                Job.job_type,
                Job.requested_by,
                Job.failure_category,
                Job.failure_summary,
                Job.completed_at,
                Job.log,
            )
            .execution_options(synchronize_session=False)
        ).all()
        reaped = len(rows)
        if rows:
            _enqueue_reaped_job_failed(db, rows, occurred_at=now)
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


def _enqueue_reaped_job_failed(db: Session, rows, *, occurred_at: datetime) -> None:
    """Stage a job.failed webhook for each job the reaper just failed. The
    reaper is the only path that fails a job without an agent result, so it
    carries its own webhook wiring (the agent result callback uses
    on_job_result)."""
    if not settings.webhooks_enabled:
        return
    host_ids = {r.host_id for r in rows}
    hostnames = dict(
        db.execute(select(Host.id, Host.hostname).where(Host.id.in_(host_ids))).all()
    )
    for r in rows:
        on_job_reaped(
            db,
            job_id=r.id,
            host_id=r.host_id,
            hostname=hostnames.get(r.host_id, ""),
            job_type=r.job_type,
            requested_by=r.requested_by,
            failure_category=r.failure_category,
            failure_summary=r.failure_summary,
            completed_at=r.completed_at,
            log_text=r.log,
            occurred_at=occurred_at,
        )


def retention_sweep(
    db: Session,
    now: datetime,
    *,
    reports_days: int,
    jobs_days: int,
    audit_days: int,
    tokens_days: int,
) -> tuple[int, int, int, int]:
    """Delete old append-only rows. 0 days = keep forever. Terminal jobs only
    (pending/running are never removed here); agent tokens only once they have
    been revoked or expired for `tokens_days`. Returns
    (reports, jobs, audit, tokens) deleted."""
    reports_deleted = 0
    jobs_deleted = 0
    audit_deleted = 0
    tokens_deleted = 0
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
    if audit_days > 0:
        cutoff = now - timedelta(days=audit_days)
        audit_deleted = db.execute(
            delete(AuditLog).where(AuditLog.at < cutoff)
        ).rowcount
    if tokens_days > 0:
        cutoff = now - timedelta(days=tokens_days)
        tokens_deleted = db.execute(
            delete(AgentToken).where(
                or_(
                    (AgentToken.revoked_at.is_not(None)) & (AgentToken.revoked_at < cutoff),
                    (AgentToken.expires_at.is_not(None)) & (AgentToken.expires_at < cutoff),
                )
            )
        ).rowcount
    db.commit()
    return reports_deleted, jobs_deleted, audit_deleted, tokens_deleted


def sweep_webhook_deliveries(db: Session, now: datetime, *, days: int) -> int:
    """Delete terminal (delivered/failed) webhook_deliveries rows older than
    `days`. 0 = keep forever. Pending rows are never removed here. Returns the
    row count deleted."""
    if days <= 0:
        return 0
    cutoff = now - timedelta(days=days)
    deleted = db.execute(
        delete(WebhookDelivery).where(
            WebhookDelivery.status.in_(("delivered", "failed")),
            WebhookDelivery.completed_at.is_not(None),
            WebhookDelivery.completed_at < cutoff,
        )
    ).rowcount
    db.commit()
    return deleted


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
    if (
        settings.reports_retention_days == 0
        and settings.jobs_retention_days == 0
        and settings.audit_retention_days == 0
        and settings.token_retention_days == 0
        and settings.webhook_deliveries_retention_days == 0
    ):
        return
    with SessionLocal() as db:
        if not _retention_due(db, now):
            return
        reports, jobs, audit, tokens = retention_sweep(
            db,
            now,
            reports_days=settings.reports_retention_days,
            jobs_days=settings.jobs_retention_days,
            audit_days=settings.audit_retention_days,
            tokens_days=settings.token_retention_days,
        )
        webhook_deliveries = sweep_webhook_deliveries(
            db, now, days=settings.webhook_deliveries_retention_days
        )
        _mark_retention_done(db, now)
    log.info(
        "retention sweep done",
        extra=_f(
            reports_deleted=reports,
            jobs_deleted=jobs,
            audit_deleted=audit,
            tokens_deleted=tokens,
            webhook_deliveries_deleted=webhook_deliveries,
            keep_reports_days=settings.reports_retention_days,
            keep_jobs_days=settings.jobs_retention_days,
            keep_audit_days=settings.audit_retention_days,
            keep_tokens_days=settings.token_retention_days,
            keep_webhook_deliveries_days=settings.webhook_deliveries_retention_days,
        ),
    )


def _advisory_due(db: Session, now: datetime) -> bool:
    """True when the advisory feed hasn't been refreshed within
    ADVISORY_REFRESH_EVERY. Last-run time lives in scheduler_state so a
    restart doesn't re-trigger a fetch."""
    last = db.execute(
        select(SchedulerState.value).where(SchedulerState.key == ADVISORY_STATE_KEY)
    ).scalar_one_or_none()
    if last is None:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return now - last_dt >= ADVISORY_REFRESH_EVERY


def _mark_advisory_done(db: Session, now: datetime) -> None:
    db.execute(
        pg_insert(SchedulerState)
        .values(key=ADVISORY_STATE_KEY, value=now.isoformat(), updated_at=now)
        .on_conflict_do_update(
            index_elements=["key"], set_={"value": now.isoformat(), "updated_at": now}
        )
    )
    db.commit()


def run_advisory_refresh_if_due(
    now: datetime | None = None, db: Session | None = None
) -> None:
    """Pull the Debian DSA/DLA feeds into `advisories` / `advisory_packages`
    at most once per ADVISORY_REFRESH_EVERY. A fetch or parse failure is
    logged and swallowed -- the last good rows stay, and the next tick retries
    because the state key is only advanced on success. Pass `db` to run inside
    an existing session (tests)."""
    now = now or datetime.now(timezone.utc)
    if not settings.advisory_refresh_enabled:
        return
    urls = settings.advisory_feed_urls or list(debian.DEFAULT_FEED_URLS)
    own_session = db is None
    db = db or SessionLocal()
    try:
        if not _advisory_due(db, now):
            return
        try:
            parsed: list[debian.ParsedAdvisory] = []
            for url in urls:
                parsed.extend(debian.parse_list(debian.fetch(url)))
        except Exception:  # noqa: BLE001 -- keep last good data, retry next tick
            log.warning(
                "advisory refresh failed, keeping last good data", exc_info=True
            )
            return
        counts = refresh_advisories(db, parsed, now)
        _mark_advisory_done(db, now)
    finally:
        if own_session:
            db.close()
    log.info("advisory refresh done", extra=_f(**counts, feeds=len(urls)))


def _packages_gc_due(db: Session, now: datetime) -> bool:
    """True when the packages GC hasn't run within PACKAGES_GC_EVERY. Last-run
    time lives in scheduler_state so a restart doesn't re-trigger it."""
    last = db.execute(
        select(SchedulerState.value).where(SchedulerState.key == PACKAGES_GC_STATE_KEY)
    ).scalar_one_or_none()
    if last is None:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return now - last_dt >= PACKAGES_GC_EVERY


def _mark_packages_gc_done(db: Session, now: datetime) -> None:
    db.execute(
        pg_insert(SchedulerState)
        .values(key=PACKAGES_GC_STATE_KEY, value=now.isoformat(), updated_at=now)
        .on_conflict_do_update(
            index_elements=["key"], set_={"value": now.isoformat(), "updated_at": now}
        )
    )
    db.commit()


def gc_orphan_packages(db: Session) -> int:
    """Delete `packages` dimension rows that no `host_packages` references.
    Every read path inner-joins `packages` from `host_packages`, so an orphan
    row is already invisible; this just stops the table growing forever."""
    deleted = db.execute(
        delete(Package).where(
            ~exists(
                select(HostPackage.package_id).where(
                    HostPackage.package_id == Package.id
                )
            )
        )
    ).rowcount
    db.commit()
    return deleted


def run_packages_gc_if_due(
    now: datetime | None = None, db: Session | None = None
) -> None:
    """Sweep orphaned `packages` rows at most once per PACKAGES_GC_EVERY. Pass
    `db` to run inside an existing session (tests)."""
    now = now or datetime.now(timezone.utc)
    if not settings.packages_gc_enabled:
        return
    own_session = db is None
    db = db or SessionLocal()
    try:
        if not _packages_gc_due(db, now):
            return
        deleted = gc_orphan_packages(db)
        _mark_packages_gc_done(db, now)
    finally:
        if own_session:
            db.close()
    log.info("packages GC done", extra=_f(packages_deleted=deleted))


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
            scan_offline_hosts()
            dispatch_pending_deliveries()
            run_retention_if_due()
            run_advisory_refresh_if_due()
            run_packages_gc_if_due()
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
