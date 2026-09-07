"""Fleet-wide summary for the dashboard overview. No auth, like the other
read views."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.staleness import LATE_AFTER, SILENT_AFTER
from app.models.models import Host, HostPackage, Job
from app.schemas.schemas import FleetSummary

router = APIRouter(prefix="/api/v1", tags=["fleet"])


@router.get("/fleet/summary", response_model=FleetSummary)
def fleet_summary(db: Session = Depends(get_db)) -> FleetSummary:
    now = datetime.now(timezone.utc)
    silent_cut = now - SILENT_AFTER
    late_cut = now - LATE_AFTER

    pending = func.count().filter(HostPackage.candidate_version.is_not(None))
    security = func.count().filter(
        and_(
            HostPackage.candidate_version.is_not(None),
            HostPackage.is_security_update.is_(True),
        )
    )
    per_host = (
        select(
            Host.is_active.label("is_active"),
            Host.reboot_required.label("reboot_required"),
            Host.last_seen_at.label("last_seen_at"),
            func.coalesce(pending, 0).label("pending"),
            func.coalesce(security, 0).label("security"),
        )
        .outerjoin(HostPackage, HostPackage.host_id == Host.id)
        .group_by(Host.id)
        .subquery()
    )
    p = per_host.c
    active = p.is_active.is_(True)
    reported = p.last_seen_at.is_not(None)
    # Same bucketing as the old Python loop: a never-reported active host counts
    # only as silent; a silent-by-age host is still classified by its updates,
    # so `silent` and `up_to_date` can both cover the same host.
    is_silent = active & (p.last_seen_at.is_(None) | (p.last_seen_at <= silent_cut))
    is_late = active & reported & (p.last_seen_at <= late_cut) & (p.last_seen_at > silent_cut)

    row = db.execute(
        select(
            func.count().label("total"),
            func.count().filter(active).label("active"),
            func.count().filter(active & reported & (p.security > 0)).label("security_hosts"),
            func.count()
            .filter(active & reported & (p.security == 0) & (p.pending > 0))
            .label("updates"),
            func.count()
            .filter(active & reported & (p.security == 0) & (p.pending == 0))
            .label("up_to_date"),
            func.count().filter(active & p.reboot_required.is_(True)).label("reboot"),
            func.count().filter(is_silent).label("silent"),
            func.count().filter(is_late).label("late"),
            func.coalesce(func.sum(p.pending).filter(active), 0).label("total_pending"),
            func.coalesce(func.sum(p.security).filter(active), 0).label("total_security"),
            func.min(p.last_seen_at).filter(active & reported).label("oldest_seen"),
        ).select_from(per_host)
    ).one()

    oldest_age = (
        None if row.oldest_seen is None else int((now - row.oldest_seen).total_seconds())
    )

    day_ago = now - timedelta(hours=24)
    running = db.scalar(select(func.count()).select_from(Job).where(Job.status == "running"))
    succeeded_24h = db.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.status == "succeeded", Job.completed_at >= day_ago)
    )
    failed_24h = db.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.status == "failed", Job.completed_at >= day_ago)
    )

    return FleetSummary(
        total_hosts=row.total,
        active_hosts=row.active,
        inactive_hosts=row.total - row.active,
        up_to_date=row.up_to_date,
        updates_available=row.updates,
        security_updates_available=row.security_hosts,
        reboot_required=row.reboot,
        late=row.late,
        silent=row.silent,
        pending_updates=row.total_pending,
        security_updates=row.total_security,
        oldest_report_age_seconds=oldest_age,
        jobs_running=running or 0,
        jobs_succeeded_24h=succeeded_24h or 0,
        jobs_failed_24h=failed_24h or 0,
    )
