"""Fleet-wide cross-package view: which hosts have a given package pending,
without opening each host. No auth, like the other read views.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, select, tuple_
from sqlalchemy.orm import Session

from app.advisories.match import advisories_for, codename_for, source_for
from app.api.deps import get_db
from app.api.pagination import after_keyset
from app.models.models import Host, HostPackage, Package
from app.schemas.schemas import PackageHostOut, PackageSummary

router = APIRouter(prefix="/api/v1", tags=["packages"])

PackageStatus = Literal["pending", "security", "all"]


@router.get("/packages", response_model=list[PackageSummary])
def list_packages(
    name: str | None = None,
    status: PackageStatus = "pending",
    limit: int = Query(50, ge=1, le=500),
    after: str | None = Query(
        None, description="page cursor: package name of the last row"
    ),
    after_id: str | None = Query(
        None,
        description="page cursor: architecture of the last row (pass with `after`)",
    ),
    db: Session = Depends(get_db),
) -> list[PackageSummary]:
    # The name / status filters go on both queries below, so a filtered page
    # walks the filtered domain exactly as an unfiltered page walks the whole.
    filters = []
    if name:
        filters.append(Package.name.ilike(f"%{name}%"))
    if status == "pending":
        filters.append(HostPackage.candidate_version.is_not(None))
    elif status == "security":
        filters.append(
            and_(
                HostPackage.candidate_version.is_not(None),
                HostPackage.is_security_update.is_(True),
            )
        )

    # 1. Which (name, architecture) groups are on this page. Keyset over the
    #    same (name, architecture) order the row query uses, so a group is
    #    never split across a page boundary. `packages` has UNIQUE(name,
    #    architecture), so that pair is a stable non-null cursor.
    page_stmt = (
        select(Package.name, Package.architecture)
        .join(HostPackage, HostPackage.package_id == Package.id)
        .join(Host, Host.id == HostPackage.host_id)
        .group_by(Package.name, Package.architecture)
        .order_by(Package.name, Package.architecture)
        .limit(limit)
    )
    if filters:
        page_stmt = page_stmt.where(*filters)
    keyset = after_keyset(Package.name, Package.architecture, after, after_id)
    if keyset is not None:
        page_stmt = page_stmt.where(keyset)
    page_keys = [(r.name, r.architecture) for r in db.execute(page_stmt).all()]
    if not page_keys:
        return []

    # 2. Every (package x host) row for those groups.
    stmt = (
        select(
            Package.name,
            Package.architecture,
            Host.id.label("host_id"),
            Host.hostname,
            Host.os_version,
            Host.os_codename,
            HostPackage.installed_version,
            HostPackage.candidate_version,
            HostPackage.is_security_update,
            HostPackage.update_origin,
            HostPackage.updated_at,
            HostPackage.source_package,
        )
        .join(HostPackage, HostPackage.package_id == Package.id)
        .join(Host, Host.id == HostPackage.host_id)
        .where(tuple_(Package.name, Package.architecture).in_(page_keys))
        .order_by(Package.name, Package.architecture, Host.hostname)
    )
    if filters:
        stmt = stmt.where(*filters)

    all_rows = db.execute(stmt).all()

    # Link each apt-flagged pending security update to the DSA/DLA(s) that fix
    # it, keyed per (package, architecture, host). Purely additive.
    adv_items = []
    for r in all_rows:
        if r.candidate_version is None or not r.is_security_update:
            continue
        codename = r.os_codename or codename_for(r.os_version)
        if codename is None:
            continue
        adv_items.append(
            (
                (r.name, r.architecture, r.host_id),
                r.source_package or source_for(r.name),
                codename,
                r.candidate_version,
            )
        )
    advisories_by_key = advisories_for(db, adv_items)

    grouped: dict[tuple[str, str], list] = {}
    for row in all_rows:
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
                    source_package=r.source_package,
                    advisories=advisories_by_key.get(
                        (r.name, r.architecture, r.host_id), []
                    ),
                )
                for r in rows
            ],
        )
        for (pkg_name, architecture), rows in grouped.items()
    ]
