"""Fleet-wide summary for the dashboard overview. No auth, like the other
read views."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.models import Host, HostPackage, Job, Report
from app.schemas.schemas import FleetSummary

router = APIRouter(prefix="/api/v1", tags=["fleet"])

# Kept in sync with frontend/src/lib/time.ts staleness buckets.
LATE_AFTER = timedelta(minutes=5)
SILENT_AFTER = timedelta(minutes=15)


@router.get("/fleet/summary", response_model=FleetSummary)
def fleet_summary(db: Session = Depends(get_db)) -> FleetSummary:
    now = datetime.now(timezone.utc)

    pending = func.count().filter(HostPackage.candidate_version.is_not(None))
    security = func.count().filter(
        and_(
            HostPackage.candidate_version.is_not(None),
            HostPackage.is_security_update.is_(True),
        )
    )
    per_host = (
        select(
            Host.id.label("host_id"),
            Host.is_active,
            Host.reboot_required,
            Host.last_seen_at,
            func.coalesce(pending, 0).label("pending"),
            func.coalesce(security, 0).label("security"),
        )
        .outerjoin(HostPackage, HostPackage.host_id == Host.id)
        .group_by(Host.id)
        .subquery()
    )
    rows = db.execute(select(per_host)).all()

    total = len(rows)
    active = [r for r in rows if r.is_active]
    up_to_date = updates = security_hosts = reboot = late = silent = 0
    total_pending = total_security = 0
    oldest_age = None
    for r in active:
        total_pending += r.pending
        total_security += r.security
        if r.reboot_required:
            reboot += 1
        # A host that has never reported has no known status -- count it only
        # as silent.
        if r.last_seen_at is None:
            silent += 1
            continue
        age = now - r.last_seen_at
        if oldest_age is None or age > oldest_age:
            oldest_age = age
        if age >= SILENT_AFTER:
            silent += 1
        elif age >= LATE_AFTER:
            late += 1
        if r.security > 0:
            security_hosts += 1
        elif r.pending > 0:
            updates += 1
        else:
            up_to_date += 1

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
        total_hosts=total,
        active_hosts=len(active),
        inactive_hosts=total - len(active),
        up_to_date=up_to_date,
        updates_available=updates,
        security_updates_available=security_hosts,
        reboot_required=reboot,
        late=late,
        silent=silent,
        pending_updates=total_pending,
        security_updates=total_security,
        oldest_report_age_seconds=None if oldest_age is None else int(oldest_age.total_seconds()),
        jobs_running=running or 0,
        jobs_succeeded_24h=succeeded_24h or 0,
        jobs_failed_24h=failed_24h or 0,
    )
