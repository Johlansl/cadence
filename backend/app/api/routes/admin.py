"""Admin provisioning endpoints, guarded by the X-Admin-Key shared secret."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.audit import record_audit
from app.api.deps import get_db, require_admin_key
from app.api.pagination import before_keyset
from app.core.config import settings
from app.core.crypto import encrypt_token_secret
from app.models.models import AgentToken, AuditLog, Host, Job
from app.schemas.schemas import (
    AuditEntry,
    HostCreate,
    HostCreated,
    HostPatched,
    HostUpdate,
    JobCreate,
    JobOut,
    TokenCreate,
    TokenIssued,
    TokenOut,
)

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin_key)],
)


def _default_token_expiry() -> datetime:
    """A token with no explicit expires_at gets this instead of NULL --
    CADENCE_TOKEN_DEFAULT_EXPIRY_DAYS, 365 by default. Explicit overrides
    (TokenCreate.expires_at) are unaffected; this only fills the gap when
    none was given."""
    return datetime.now(timezone.utc) + timedelta(days=settings.token_default_expiry_days)


@router.post("/hosts", response_model=HostCreated, status_code=status.HTTP_201_CREATED)
def create_host(
    request: Request, payload: HostCreate, db: Session = Depends(get_db)
) -> HostCreated:
    # Generate the agent's first token. Its hash is stored for the legacy
    # bearer path and an encrypted copy for the signed-request path (see
    # app.core.crypto); no override param here, this token always gets the
    # default expiry. Further tokens are issued/revoked via
    # /admin/hosts/{id}/tokens.
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    host = Host(
        hostname=payload.hostname,
        fqdn=payload.fqdn,
        description=payload.description,
    )
    db.add(host)
    db.flush()  # populate host.id for the token + audit rows
    db.add(
        AgentToken(
            host_id=host.id,
            token_hash=token_hash,
            secret_encrypted=encrypt_token_secret(token),
            label="initial",
            expires_at=_default_token_expiry(),
        )
    )
    record_audit(
        db, request, "host.create", target_type="host", target_id=host.id,
        detail={"hostname": host.hostname},
    )
    db.commit()
    db.refresh(host)

    return HostCreated(id=host.id, hostname=host.hostname, token=token)


@router.patch("/hosts/{host_id}", response_model=HostPatched)
def update_host(
    request: Request,
    host_id: uuid.UUID,
    payload: HostUpdate,
    db: Session = Depends(get_db),
) -> Host:
    host = db.get(Host, host_id)
    if host is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "host not found")

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "no fields to update")
    for field, value in changes.items():
        setattr(host, field, value)
    host.updated_at = datetime.now(timezone.utc)
    record_audit(
        db, request, "host.update", target_type="host", target_id=host_id,
        detail={"fields": sorted(changes)},
    )
    db.commit()
    db.refresh(host)
    return host


@router.delete("/hosts/{host_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_host(
    request: Request, host_id: uuid.UUID, db: Session = Depends(get_db)
) -> Response:
    # Permanent: cascades to host_packages / reports / jobs / schedules.
    # Prefer PATCH {"is_active": false} to keep the history.
    host = db.get(Host, host_id)
    if host is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "host not found")
    hostname = host.hostname
    db.delete(host)
    # audit_log has no FK to hosts, so this row outlives the cascade.
    record_audit(
        db, request, "host.delete", target_type="host", target_id=host_id,
        detail={"hostname": hostname},
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/hosts/{host_id}/jobs",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
)
def create_job(
    request: Request,
    host_id: uuid.UUID,
    payload: JobCreate,
    db: Session = Depends(get_db),
) -> Job:
    if db.get(Host, host_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "host not found")

    # One active job per host: the agent runs them serially and the dashboard
    # button is disabled while one is in flight.
    active = db.execute(
        select(Job.id)
        .where(Job.host_id == host_id, Job.status.in_(("pending", "running")))
        .limit(1)
    ).first()
    if active is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "a job is already pending or running for this host",
        )

    job = Job(
        host_id=host_id,
        job_type=payload.job_type,
        params=payload.params,
        requested_by=payload.requested_by,
    )
    db.add(job)
    db.flush()  # populate job.id for the audit row
    record_audit(
        db, request, "job.create", target_type="job", target_id=job.id,
        detail={
            "host_id": str(host_id),
            "job_type": job.job_type,
            "params": job.params,
        },
    )
    db.commit()
    db.refresh(job)
    return job


@router.delete("/hosts/{host_id}/jobs", status_code=status.HTTP_204_NO_CONTENT)
def clear_host_jobs(
    request: Request, host_id: uuid.UUID, db: Session = Depends(get_db)
) -> Response:
    # Wipe the job history for a host (all statuses). If a job is mid-run the
    # agent's later result callback just 404s -- harmless.
    if db.get(Host, host_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "host not found")
    deleted = db.execute(delete(Job).where(Job.host_id == host_id)).rowcount
    record_audit(
        db, request, "job.clear", target_type="host", target_id=host_id,
        detail={"deleted": deleted},
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _token_state(tok: AgentToken, now: datetime) -> str:
    if tok.revoked_at is not None:
        return "revoked"
    if tok.expires_at is not None and tok.expires_at <= now:
        return "expired"
    return "active"


@router.get("/hosts/{host_id}/tokens", response_model=list[TokenOut])
def list_host_tokens(host_id: uuid.UUID, db: Session = Depends(get_db)) -> list[TokenOut]:
    """Every agent token for the host -- active, expired and revoked -- so a
    stale one is obvious at a glance. The hash is never returned."""
    if db.get(Host, host_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "host not found")
    now = datetime.now(timezone.utc)
    rows = db.execute(
        select(AgentToken).where(AgentToken.host_id == host_id)
    ).scalars().all()
    out = [
        TokenOut(
            id=t.id,
            label=t.label,
            created_at=t.created_at,
            last_used_at=t.last_used_at,
            expires_at=t.expires_at,
            revoked_at=t.revoked_at,
            state=_token_state(t, now),
        )
        for t in rows
    ]
    # active first, then newest first (id is monotonic, unlike a shared now())
    out.sort(key=lambda t: (t.state != "active", -t.id))
    return out


@router.post(
    "/hosts/{host_id}/tokens",
    response_model=TokenIssued,
    status_code=status.HTTP_201_CREATED,
)
def issue_host_token(
    request: Request,
    host_id: uuid.UUID,
    payload: TokenCreate,
    db: Session = Depends(get_db),
) -> TokenIssued:
    """Issue an additional token so the agent can be rotated onto it before the
    old one is revoked. The plaintext is returned once."""
    if db.get(Host, host_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "host not found")

    secret = secrets.token_urlsafe(32)
    tok = AgentToken(
        host_id=host_id,
        token_hash=hashlib.sha256(secret.encode()).hexdigest(),
        secret_encrypted=encrypt_token_secret(secret),
        label=payload.label,
        expires_at=payload.expires_at or _default_token_expiry(),
    )
    db.add(tok)
    db.flush()  # populate tok.id for the audit row
    record_audit(
        db, request, "token.issue", target_type="token", target_id=tok.id,
        detail={
            "host_id": str(host_id),
            "label": tok.label,
            "expires_at": tok.expires_at.isoformat() if tok.expires_at else None,
        },
    )
    db.commit()
    db.refresh(tok)
    return TokenIssued(
        id=tok.id, label=tok.label, expires_at=tok.expires_at, token=secret
    )


@router.delete(
    "/hosts/{host_id}/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT
)
def revoke_host_token(
    request: Request, host_id: uuid.UUID, token_id: int, db: Session = Depends(get_db)
) -> Response:
    """Revoke one token (auth-plane only -- pending/running jobs are untouched).
    Idempotent: revoking an already-revoked token is a no-op 204."""
    tok = db.get(AgentToken, token_id)
    if tok is None or tok.host_id != host_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "token not found")
    if tok.revoked_at is None:
        tok.revoked_at = datetime.now(timezone.utc)
        record_audit(
            db, request, "token.revoke", target_type="token", target_id=tok.id,
            detail={"host_id": str(host_id), "label": tok.label},
        )
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/audit", response_model=list[AuditEntry])
def list_audit(
    limit: int = Query(50, ge=1, le=500),
    action: str | None = Query(None, description="exact match on the action verb"),
    target_type: str | None = Query(None),
    target_id: str | None = Query(None),
    before: datetime | None = Query(
        None, description="page cursor: `at` of the last row you have"
    ),
    before_id: int | None = Query(
        None, description="page cursor: `id` of the last row you have (pass with `before`)"
    ),
    db: Session = Depends(get_db),
) -> list[AuditLog]:
    """The admin audit trail, newest first. Guarded by X-Admin-Key like the
    writes it records. Page with (`before`, `before_id`) = the `at` and `id`
    of the oldest row you already have."""
    stmt = select(AuditLog)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if target_type is not None:
        stmt = stmt.where(AuditLog.target_type == target_type)
    if target_id is not None:
        stmt = stmt.where(AuditLog.target_id == target_id)
    keyset = before_keyset(AuditLog.at, AuditLog.id, before, before_id)
    if keyset is not None:
        stmt = stmt.where(keyset)
    stmt = stmt.order_by(AuditLog.at.desc(), AuditLog.id.desc()).limit(limit)
    return list(db.execute(stmt).scalars().all())
