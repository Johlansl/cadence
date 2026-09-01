"""Read-only host views for the dashboard.

No auth in V1: Cadence is single-user and these endpoints are meant to sit
behind the reverse proxy / on a trusted network (see SECURITY.md).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.models import Host, HostPackage, Package
from app.schemas.schemas import HostDetail, HostPackageOut, HostStatus, HostSummary

router = APIRouter(prefix="/api/v1", tags=["hosts"])


def _status(updates_available: int, security_updates: int) -> HostStatus:
    if security_updates > 0:
        return HostStatus.security_updates_available
    if updates_available > 0:
        return HostStatus.updates_available
    return HostStatus.up_to_date


def _summary_fields(host: Host) -> dict:
    return {
        "id": host.id,
        "hostname": host.hostname,
        "fqdn": host.fqdn,
        "description": host.description,
        "os_family": host.os_family,
        "os_name": host.os_name,
        "os_version": host.os_version,
        "package_manager": host.package_manager,
        "agent_version": host.agent_version,
        "reboot_required": host.reboot_required,
        "reboot_policy": host.reboot_policy,
        "is_active": host.is_active,
        "tags": host.tags or {},
        "last_seen_at": host.last_seen_at,
        "created_at": host.created_at,
        "updated_at": host.updated_at,
    }


@router.get("/hosts", response_model=list[HostSummary])
def list_hosts(db: Session = Depends(get_db)) -> list[HostSummary]:
    pending = func.count().filter(HostPackage.candidate_version.is_not(None))
    security = func.count().filter(
        and_(
            HostPackage.candidate_version.is_not(None),
            HostPackage.is_security_update.is_(True),
        )
    )
    stmt = (
        select(
            Host,
            func.coalesce(pending, 0).label("updates_available_count"),
            func.coalesce(security, 0).label("security_updates_count"),
        )
        .outerjoin(HostPackage, HostPackage.host_id == Host.id)
        .group_by(Host.id)
        .order_by(Host.hostname, Host.created_at)
    )

    return [
        HostSummary(
            **_summary_fields(host),
            status=_status(updates_available, security_updates),
            updates_available_count=updates_available,
            security_updates_count=security_updates,
        )
        for host, updates_available, security_updates in db.execute(stmt).all()
    ]


@router.get("/hosts/{host_id}", response_model=HostDetail)
def get_host(host_id: uuid.UUID, db: Session = Depends(get_db)) -> HostDetail:
    host = db.get(Host, host_id)
    if host is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "host not found")

    pkg_rows = db.execute(
        select(
            Package.name,
            Package.architecture,
            HostPackage.installed_version,
            HostPackage.candidate_version,
            HostPackage.is_security_update,
            HostPackage.update_origin,
            HostPackage.updated_at,
        )
        .join(Package, Package.id == HostPackage.package_id)
        .where(HostPackage.host_id == host.id)
        .order_by(Package.name, Package.architecture)
    ).all()

    updates_available = sum(1 for r in pkg_rows if r.candidate_version is not None)
    security_updates = sum(
        1 for r in pkg_rows if r.candidate_version is not None and r.is_security_update
    )

    return HostDetail(
        **_summary_fields(host),
        status=_status(updates_available, security_updates),
        updates_available_count=updates_available,
        security_updates_count=security_updates,
        packages=[HostPackageOut(**r._mapping) for r in pkg_rows],
    )
