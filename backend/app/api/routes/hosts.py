"""Read-only host views for the dashboard.

No auth in V1: Cadence is single-user and these endpoints are meant to sit
behind the reverse proxy / on a trusted network (see SECURITY.md).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from app.advisories.match import advisories_for, codename_for, source_for
from app.api.deps import get_db
from app.api.pagination import after_keyset
from app.core.staleness import LATE_AFTER
from app.exclusions import matching, patterns_for_host
from app.models.models import Host, HostPackage, Package, PackageExclusion
from app.schemas.schemas import HostDetail, HostPackageOut, HostStatus, HostSummary

router = APIRouter(prefix="/api/v1", tags=["hosts"])

HostStatusFilter = Literal["all", "security", "updates", "uptodate"]
FreshnessFilter = Literal["all", "silent"]


def _status(updates_available: int, security_updates: int) -> HostStatus:
    if security_updates > 0:
        return HostStatus.security_updates_available
    if updates_available > 0:
        return HostStatus.updates_available
    return HostStatus.up_to_date


def _excluded_counts(db: Session, host_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """How many pending updates a package_exclusions rule matches, per host.
    Bounded to host_ids (the already-paginated page), so this stays cheap
    regardless of fleet size. A host absent from the returned dict has 0."""
    if not host_ids:
        return {}
    global_patterns = list(
        db.execute(
            select(PackageExclusion.pattern).where(PackageExclusion.scope == "global")
        )
        .scalars()
        .all()
    )
    host_patterns: dict[uuid.UUID, list[str]] = {}
    for hid, pattern in db.execute(
        select(PackageExclusion.host_id, PackageExclusion.pattern).where(
            PackageExclusion.scope == "host", PackageExclusion.host_id.in_(host_ids)
        )
    ).all():
        host_patterns.setdefault(hid, []).append(pattern)
    if not global_patterns and not host_patterns:
        return {}

    names_by_host: dict[uuid.UUID, list[str]] = {}
    for hid, name in db.execute(
        select(HostPackage.host_id, Package.name)
        .join(Package, Package.id == HostPackage.package_id)
        .where(
            HostPackage.host_id.in_(host_ids),
            HostPackage.candidate_version.is_not(None),
        )
    ).all():
        names_by_host.setdefault(hid, []).append(name)

    return {
        hid: len(matching(names, global_patterns + host_patterns.get(hid, [])))
        for hid, names in names_by_host.items()
    }


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
def list_hosts(
    q: str | None = Query(None, description="substring match on hostname or description"),
    status: HostStatusFilter = "all",
    tag: str | None = Query(
        None, description='"key" (key or value contains it) or "key=value" (exact pair)'
    ),
    include_inactive: bool = Query(
        True, description="default returns retired hosts too; false hides them"
    ),
    freshness: FreshnessFilter = Query(
        "all", description='"silent" keeps only hosts not seen in the last 5 minutes'
    ),
    limit: int | None = Query(None, ge=1, le=200, description="omit for the full list"),
    after: str | None = Query(None, description="page cursor: hostname of the last row"),
    after_id: uuid.UUID | None = Query(
        None, description="page cursor: id of the last row (pass with `after`)"
    ),
    db: Session = Depends(get_db),
) -> list[HostSummary]:
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
        .order_by(Host.hostname, Host.id)
    )

    if not include_inactive:
        stmt = stmt.where(Host.is_active.is_(True))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Host.hostname.ilike(like), Host.description.ilike(like)))
    if freshness == "silent":
        cutoff = datetime.now(timezone.utc) - LATE_AFTER
        stmt = stmt.where(
            or_(Host.last_seen_at.is_(None), Host.last_seen_at <= cutoff)
        )
    if tag:
        t = tag.strip().lower()
        kv = func.jsonb_each_text(Host.tags).table_valued("key", "value")
        if "=" in t:
            k, v = t.split("=", 1)
            match = and_(func.lower(kv.c.key) == k, func.lower(kv.c.value) == v)
        else:
            like = f"%{t}%"
            match = or_(func.lower(kv.c.key).like(like), func.lower(kv.c.value).like(like))
        stmt = stmt.where(exists(select(1).select_from(kv).where(match)))
    if status == "security":
        stmt = stmt.having(security > 0)
    elif status == "updates":
        stmt = stmt.having(pending > 0)
    elif status == "uptodate":
        stmt = stmt.having(pending == 0)

    keyset = after_keyset(Host.hostname, Host.id, after, after_id)
    if keyset is not None:
        stmt = stmt.where(keyset)
    if limit is not None:
        stmt = stmt.limit(limit)

    rows = db.execute(stmt).all()
    excluded = _excluded_counts(db, [host.id for host, _, _ in rows])

    return [
        HostSummary(
            **_summary_fields(host),
            status=_status(updates_available, security_updates),
            updates_available_count=updates_available,
            security_updates_count=security_updates,
            excluded_count=excluded.get(host.id, 0),
        )
        for host, updates_available, security_updates in rows
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
            HostPackage.source_package,
        )
        .join(Package, Package.id == HostPackage.package_id)
        .where(HostPackage.host_id == host.id)
        .order_by(Package.name, Package.architecture)
    ).all()

    updates_available = sum(1 for r in pkg_rows if r.candidate_version is not None)
    security_updates = sum(
        1 for r in pkg_rows if r.candidate_version is not None and r.is_security_update
    )

    # excluded is computed against every package (mirrors is_security_update,
    # shown per row regardless of a pending update -- a hold is meaningful
    # even before a candidate exists); excluded_count only tallies pending
    # ones, matching "N of the M available updates are excluded".
    excluded_names = set(matching((r.name for r in pkg_rows), patterns_for_host(db, host.id)))
    excluded_count = sum(
        1 for r in pkg_rows if r.candidate_version is not None and r.name in excluded_names
    )

    # Link each apt-flagged pending security update to the DSA/DLA(s) that fix
    # it. Never affects the counts above -- purely additive metadata.
    advisories_by_key: dict[tuple[str, str], list] = {}
    codename = host.os_codename or codename_for(host.os_version)
    if codename:
        advisories_by_key = advisories_for(
            db,
            (
                (
                    (r.name, r.architecture),
                    r.source_package or source_for(r.name),
                    codename,
                    r.candidate_version,
                )
                for r in pkg_rows
                if r.candidate_version is not None and r.is_security_update
            ),
        )

    return HostDetail(
        **_summary_fields(host),
        status=_status(updates_available, security_updates),
        updates_available_count=updates_available,
        security_updates_count=security_updates,
        excluded_count=excluded_count,
        packages=[
            HostPackageOut(
                **r._mapping,
                advisories=advisories_by_key.get((r.name, r.architecture), []),
                excluded=r.name in excluded_names,
            )
            for r in pkg_rows
        ],
    )
