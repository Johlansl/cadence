"""Package exclusion (hold) rules.

Read views are unauthenticated like the other dashboard reads; create/delete
need the X-Admin-Key. No update route: a rule is created or deleted, not
edited in place (see migration 0015). Resolving these rules into an exact
package list for a job is app.exclusions, not here.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.audit import record_audit
from app.api.deps import get_db, require_admin_key
from app.models.models import Host, PackageExclusion
from app.schemas.schemas import PackageExclusionCreate, PackageExclusionOut

router = APIRouter(prefix="/api/v1", tags=["exclusions"])
admin_router = APIRouter(
    prefix="/api/v1/admin", tags=["exclusions"], dependencies=[Depends(require_admin_key)]
)


@router.get("/package-exclusions", response_model=list[PackageExclusionOut])
def list_exclusions(
    host_id: uuid.UUID | None = None, db: Session = Depends(get_db)
) -> list[PackageExclusion]:
    stmt = select(PackageExclusion).order_by(PackageExclusion.created_at)
    if host_id is not None:
        stmt = stmt.where(
            (PackageExclusion.scope == "global") | (PackageExclusion.host_id == host_id)
        )
    return list(db.execute(stmt).scalars().all())


@router.get("/package-exclusions/{exclusion_id}", response_model=PackageExclusionOut)
def get_exclusion(exclusion_id: uuid.UUID, db: Session = Depends(get_db)) -> PackageExclusion:
    row = db.get(PackageExclusion, exclusion_id)
    if row is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "exclusion not found")
    return row


@admin_router.post(
    "/package-exclusions",
    response_model=PackageExclusionOut,
    status_code=http_status.HTTP_201_CREATED,
)
def create_exclusion(
    request: Request, payload: PackageExclusionCreate, db: Session = Depends(get_db)
) -> PackageExclusion:
    if payload.host_id is not None and db.get(Host, payload.host_id) is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "host not found")
    row = PackageExclusion(
        scope=payload.scope,
        host_id=payload.host_id,
        pattern=payload.pattern,
        description=payload.description,
    )
    db.add(row)
    db.flush()  # populate row.id for the audit entry
    record_audit(
        db,
        request,
        "package_exclusion.create",
        target_type="package_exclusion",
        target_id=row.id,
        detail={
            "scope": row.scope,
            "host_id": str(row.host_id) if row.host_id else None,
            "pattern": row.pattern,
        },
    )
    db.commit()
    db.refresh(row)
    return row


@admin_router.delete(
    "/package-exclusions/{exclusion_id}", status_code=http_status.HTTP_204_NO_CONTENT
)
def delete_exclusion(
    request: Request, exclusion_id: uuid.UUID, db: Session = Depends(get_db)
) -> Response:
    row = db.get(PackageExclusion, exclusion_id)
    if row is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "exclusion not found")
    db.delete(row)
    record_audit(
        db,
        request,
        "package_exclusion.delete",
        target_type="package_exclusion",
        target_id=exclusion_id,
        detail={
            "scope": row.scope,
            "host_id": str(row.host_id) if row.host_id else None,
            "pattern": row.pattern,
        },
    )
    db.commit()
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)
