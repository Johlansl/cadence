"""Fleet-wide cross-package view: which hosts have a given package pending,
without opening each host. No auth, like the other read views.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.models import Host, HostPackage, Package
from app.schemas.schemas import PackageHostOut, PackageSummary

router = APIRouter(prefix="/api/v1", tags=["packages"])

PackageStatus = Literal["pending", "security", "all"]


@router.get("/packages", response_model=list[PackageSummary])
def list_packages(
    name: str | None = None,
    status: PackageStatus = "pending",
    db: Session = Depends(get_db),
) -> list[PackageSummary]:
    stmt = (
        select(
            Package.name,
            Package.architecture,
            Host.id.label("host_id"),
            Host.hostname,
            HostPackage.installed_version,
            HostPackage.candidate_version,
            HostPackage.is_security_update,
            HostPackage.update_origin,
            HostPackage.updated_at,
        )
        .join(HostPackage, HostPackage.package_id == Package.id)
        .join(Host, Host.id == HostPackage.host_id)
        .order_by(Package.name, Package.architecture, Host.hostname)
    )
    if name:
        stmt = stmt.where(Package.name.ilike(f"%{name}%"))
    if status == "pending":
        stmt = stmt.where(HostPackage.candidate_version.is_not(None))
    elif status == "security":
        stmt = stmt.where(
            and_(
                HostPackage.candidate_version.is_not(None),
                HostPackage.is_security_update.is_(True),
            )
        )

    grouped: dict[tuple[str, str], list] = {}
    for row in db.execute(stmt).all():
        grouped.setdefault((row.name, row.architecture), []).append(row)

    return [
        PackageSummary(
            name=pkg_name,
            architecture=architecture,
            hosts=[
                PackageHostOut(
                    host_id=r.host_id,
                    hostname=r.hostname,
                    installed_version=r.installed_version,
                    candidate_version=r.candidate_version,
                    is_security_update=r.is_security_update,
                    update_origin=r.update_origin,
                    updated_at=r.updated_at,
                )
                for r in rows
            ],
        )
        for (pkg_name, architecture), rows in grouped.items()
    ]
