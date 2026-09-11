"""Administrative lifecycle for short-lived, single-use enrollment codes."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.audit import record_audit
from app.api.deps import get_db, require_admin_key
from app.core.config import settings
from app.core.crypto import encrypt_token_secret
from app.core.ratelimit import note_rejected, ratelimiter
from app.core.throttle import client_ip, throttle
from app.enrollment import generate_enrollment_code, parse_enrollment_code, server_ca_fingerprint
from app.models.models import AgentCertificate, AgentToken, AuditLog, EnrollmentCode, Host
from app.pki.client_ca import issue_client_certificate
from app.schemas.schemas import (
    EnrollmentClaim,
    EnrollmentClaimed,
    EnrollmentCreate,
    EnrollmentCreated,
    EnrollmentOut,
)

admin_router = APIRouter(
    prefix="/api/v1/admin/enrollments",
    tags=["admin", "enrollment"],
    dependencies=[Depends(require_admin_key)],
)
agent_router = APIRouter(prefix="/api/v1/agent", tags=["agent", "enrollment"])

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


async def _reject_enrollment(request: Request) -> None:
    ip = client_ip(request)
    delay = throttle.record_failure(ip, kind="enrollment")
    if delay:
        await asyncio.sleep(delay)
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid enrollment code")


@agent_router.post("/enroll", response_model=EnrollmentClaimed)
async def claim_enrollment(
    request: Request, payload: EnrollmentClaim, db: Session = Depends(get_db)
) -> EnrollmentClaimed:
    """Atomically consume one code and issue both HMAC and mTLS credentials."""
    ip = client_ip(request)
    retry_after = ratelimiter.check(
        f"enrollment:{ip}",
        limit=settings.ratelimit_enrollment_max,
        window=settings.ratelimit_window_seconds,
    )
    if retry_after:
        note_rejected(
            "enrollment", ip, settings.ratelimit_enrollment_max, retry_after, request.url.path
        )
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate limit exceeded",
            headers={"Retry-After": str(int(retry_after))},
        )

    try:
        parts = parse_enrollment_code(payload.code)
        current_fingerprint = server_ca_fingerprint(settings.server_ca_file)
    except (ValueError, RuntimeError):
        await _reject_enrollment(request)

    now = datetime.now(timezone.utc)
    row = db.execute(
        select(EnrollmentCode)
        .where(EnrollmentCode.secret_hash == parts.secret_hash)
        .with_for_update()
    ).scalar_one_or_none()
    if (
        row is None
        or row.consumed_at is not None
        or row.revoked_at is not None
        or row.expires_at <= now
        or not secrets.compare_digest(
            parts.ca_fingerprint_sha256, row.ca_fingerprint_sha256
        )
        or not secrets.compare_digest(parts.ca_fingerprint_sha256, current_fingerprint)
    ):
        await _reject_enrollment(request)

    if row.target_host_id is None:
        if not secrets.compare_digest(payload.hostname, row.expected_hostname or ""):
            await _reject_enrollment(request)
        host = Host(
            hostname=payload.hostname,
            fqdn=payload.fqdn,
            description=row.description,
            tags=row.tags,
            reboot_policy=row.reboot_policy,
        )
        db.add(host)
        db.flush()
    else:
        host = db.execute(
            select(Host).where(Host.id == row.target_host_id).with_for_update()
        ).scalar_one_or_none()
        if (
            host is None
            or not host.is_active
            or not secrets.compare_digest(payload.hostname, host.hostname)
        ):
            await _reject_enrollment(request)

    try:
        issued = issue_client_certificate(settings.client_pki_dir, payload.csr_pem, host.id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    token = secrets.token_urlsafe(32)
    db.add(
        AgentToken(
            host_id=host.id,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            secret_encrypted=encrypt_token_secret(token),
            label="enrollment",
            expires_at=now + timedelta(days=settings.token_default_expiry_days),
        )
    )
    db.add(
        AgentCertificate(
            host_id=host.id,
            enrollment_code_id=row.id,
            serial_number=issued.serial_number,
            fingerprint_sha256=issued.fingerprint_sha256,
            not_before=issued.not_before,
            expires_at=issued.expires_at,
        )
    )
    row.enrolled_host_id = host.id
    row.consumed_at = now
    db.add(
        AuditLog(
            action="enrollment.consume",
            target_type="enrollment_code",
            target_id=str(row.id),
            actor="agent-enrollment",
            client=ip,
            request_id=getattr(request.state, "request_id", None),
            detail={"host_id": str(host.id)},
        )
    )
    db.commit()
    throttle.record_success(ip)
    return EnrollmentClaimed(
        host_id=host.id,
        server_url=f"https://{settings.site_address}:{settings.agent_port}",
        token=token,
        client_certificate_pem=issued.certificate_chain_pem,
        client_certificate_expires_at=issued.expires_at,
    )
