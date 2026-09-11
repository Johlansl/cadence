"""Administrative lifecycle for short-lived, single-use enrollment codes."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.audit import record_audit
from app.api.deps import get_db, require_admin_key
from app.core.config import settings
from app.enrollment import generate_enrollment_code
from app.models.models import EnrollmentCode, Host
from app.schemas.schemas import EnrollmentCreate, EnrollmentCreated, EnrollmentOut

admin_router = APIRouter(
    prefix="/api/v1/admin/enrollments",
    tags=["admin", "enrollment"],
    dependencies=[Depends(require_admin_key)],
)

EnrollmentStateFilter = Literal["pending", "expired", "consumed", "revoked"]


def _state(row: EnrollmentCode, now: datetime) -> EnrollmentStateFilter:
    if row.consumed_at is not None:
        return "consumed"
    if row.revoked_at is not None:
        return "revoked"
    if row.expires_at <= now:
        return "expired"
    return "pending"


def _out(row: EnrollmentCode, now: datetime) -> EnrollmentOut:
    return EnrollmentOut(
        id=row.id,
        target_host_id=row.target_host_id,
        enrolled_host_id=row.enrolled_host_id,
        expected_hostname=row.expected_hostname,
        label=row.label,
        description=row.description,
        tags=row.tags,
        reboot_policy=row.reboot_policy,
        created_at=row.created_at,
        expires_at=row.expires_at,
        consumed_at=row.consumed_at,
        revoked_at=row.revoked_at,
        state=_state(row, now),
    )


@admin_router.post("", response_model=EnrollmentCreated, status_code=status.HTTP_201_CREATED)
def create_enrollment(
    request: Request, payload: EnrollmentCreate, db: Session = Depends(get_db)
) -> EnrollmentCreated:
    if payload.target_host_id is not None:
        host = db.get(Host, payload.target_host_id)
        if host is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "host not found")
        if not host.is_active:
            raise HTTPException(status.HTTP_409_CONFLICT, "host is inactive")

    try:
        code, parts = generate_enrollment_code(settings.server_ca_file)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    ttl_minutes = payload.ttl_minutes or settings.enrollment_default_ttl_minutes
    row = EnrollmentCode(
        secret_hash=parts.secret_hash,
        ca_fingerprint_sha256=parts.ca_fingerprint_sha256,
        target_host_id=payload.target_host_id,
        expected_hostname=payload.expected_hostname,
        label=payload.label,
        description=payload.description,
        tags=payload.tags,
        reboot_policy=payload.reboot_policy,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes),
    )
    db.add(row)
    db.flush()
    record_audit(
        db,
        request,
        "enrollment.create",
        target_type="enrollment_code",
        target_id=row.id,
        detail={
            "target_host_id": str(row.target_host_id) if row.target_host_id else None,
            "expected_hostname": row.expected_hostname,
            "expires_at": row.expires_at.isoformat(),
        },
    )
    db.commit()
    db.refresh(row)
    return EnrollmentCreated(
        id=row.id,
        code=code,
        expires_at=row.expires_at,
        target_host_id=row.target_host_id,
        expected_hostname=row.expected_hostname,
    )


@admin_router.get("", response_model=list[EnrollmentOut])
def list_enrollments(
    state_filter: EnrollmentStateFilter | None = Query(None, alias="state"),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[EnrollmentOut]:
    now = datetime.now(timezone.utc)
    stmt = select(EnrollmentCode)
    if state_filter == "pending":
        stmt = stmt.where(
            EnrollmentCode.consumed_at.is_(None),
            EnrollmentCode.revoked_at.is_(None),
            EnrollmentCode.expires_at > now,
        )
    elif state_filter == "expired":
        stmt = stmt.where(
            EnrollmentCode.consumed_at.is_(None),
            EnrollmentCode.revoked_at.is_(None),
            EnrollmentCode.expires_at <= now,
        )
    elif state_filter == "consumed":
        stmt = stmt.where(EnrollmentCode.consumed_at.is_not(None))
    elif state_filter == "revoked":
        stmt = stmt.where(EnrollmentCode.revoked_at.is_not(None))
    else:
        terminal = or_(
            EnrollmentCode.consumed_at.is_not(None),
            EnrollmentCode.revoked_at.is_not(None),
            EnrollmentCode.expires_at <= now,
        )
        stmt = stmt.order_by(terminal.asc(), EnrollmentCode.created_at.desc())
    if state_filter is not None:
        stmt = stmt.order_by(EnrollmentCode.created_at.desc())
    rows = db.execute(stmt.limit(limit)).scalars().all()
    return [_out(row, now) for row in rows]


@admin_router.delete("/{enrollment_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_enrollment(
    request: Request, enrollment_id: uuid.UUID, db: Session = Depends(get_db)
) -> Response:
    row = db.get(EnrollmentCode, enrollment_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "enrollment code not found")
    if row.consumed_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "enrollment code was already consumed")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        record_audit(
            db,
            request,
            "enrollment.revoke",
            target_type="enrollment_code",
            target_id=row.id,
        )
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
