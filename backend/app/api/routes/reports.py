"""Agent report ingestion: replaces the host's current package state and logs
the raw report."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import delete, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.api.deps import get_current_host, get_db
from app.models.models import Host, HostPackage, Job, Package, Report
from app.schemas.schemas import JobHandoff, ReportAccepted, ReportIn

router = APIRouter(prefix="/api/v1", tags=["reports"])


def _resolve_package_ids(
    db: Session, keys: set[tuple[str, str]]
) -> dict[tuple[str, str], int]:
    """Get-or-create rows in the shared `packages` dimension, return a
    (name, architecture) -> id map for the requested keys."""
    if not keys:
        return {}

    db.execute(
        pg_insert(Package)
        .values([{"name": name, "architecture": arch} for name, arch in keys])
        .on_conflict_do_nothing(index_elements=["name", "architecture"])
    )
    names = {name for name, _ in keys}
    rows = db.execute(select(Package).where(Package.name.in_(names))).scalars().all()
    return {(p.name, p.architecture): p.id for p in rows if (p.name, p.architecture) in keys}


@router.post("/reports", response_model=ReportAccepted)
def create_report(
    report_in: ReportIn,
    host: Host = Depends(get_current_host),
    db: Session = Depends(get_db),
) -> ReportAccepted:
    # Deduplicate packages by (name, architecture); last occurrence wins.
    by_key = {(p.name, p.architecture): p for p in report_in.packages}
    packages = list(by_key.values())

    now = datetime.now(timezone.utc)

    # 1. Refresh host metadata from the agent's view.
    host.hostname = report_in.hostname or host.hostname
    host.fqdn = report_in.fqdn
    host.os_family = report_in.os_family or host.os_family
    host.os_name = report_in.os_name
    host.os_version = report_in.os_version
    host.package_manager = report_in.package_manager or host.package_manager
    host.agent_version = report_in.agent_version
    host.reboot_required = report_in.reboot_required
    host.last_seen_at = now
    host.updated_at = now

    # 2. Resolve / create the referenced packages.
    key_to_id = _resolve_package_ids(db, set(by_key.keys()))

    # 3. Replace the host's current package state.
    db.execute(delete(HostPackage).where(HostPackage.host_id == host.id))
    db.flush()
    if packages:
        db.execute(
            insert(HostPackage),
            [
                {
                    "host_id": host.id,
                    "package_id": key_to_id[(p.name, p.architecture)],
                    "installed_version": p.installed_version,
                    "candidate_version": p.candidate_version,
                    "is_security_update": p.is_security_update,
                    "update_origin": p.update_origin,
                    "updated_at": now,
                }
                for p in packages
            ],
        )

    # 4. Log the raw report along with pre-computed counters.
    updates_available = sum(1 for p in packages if p.candidate_version is not None)
    security_updates = sum(
        1 for p in packages if p.is_security_update and p.candidate_version is not None
    )
    db.add(
        Report(
            host_id=host.id,
            received_at=now,
            agent_version=report_in.agent_version,
            installed_package_count=len(packages),
            updates_available_count=updates_available,
            security_updates_count=security_updates,
            reboot_required=report_in.reboot_required,
            raw_payload=report_in.model_dump(mode="json"),
        )
    )

    # 5. Piggyback: hand the oldest pending job (if any) to the agent and mark
    #    it running. One job per report; the agent runs them serially.
    job = db.execute(
        select(Job)
        .where(Job.host_id == host.id, Job.status == "pending")
        .order_by(Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    handoff = None
    if job is not None:
        job.status = "running"
        job.started_at = now
        handoff = JobHandoff(id=job.id, job_type=job.job_type, params=job.params)

    db.commit()

    return ReportAccepted(
        host_id=host.id,
        installed_package_count=len(packages),
        updates_available_count=updates_available,
        security_updates_count=security_updates,
        reboot_required=report_in.reboot_required,
        job=handoff,
    )
