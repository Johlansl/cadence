"""Admin provisioning endpoints, guarded by the X-Admin-Key shared secret."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.audit import record_audit
from app.api.deps import get_db, require_admin_key
from app.models.models import Host, Job
from app.schemas.schemas import (
    HostCreate,
    HostCreated,
    HostPatched,
    HostUpdate,
    JobCreate,
    JobOut,
)

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin_key)],
)


@router.post("/hosts", response_model=HostCreated, status_code=status.HTTP_201_CREATED)
def create_host(
    request: Request, payload: HostCreate, db: Session = Depends(get_db)
) -> HostCreated:
    # Generate the agent token; only its sha256 hash is ever stored.
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    host = Host(
        hostname=payload.hostname,
        fqdn=payload.fqdn,
        description=payload.description,
        token_hash=token_hash,
    )
    db.add(host)
    db.flush()  # populate host.id for the audit row
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
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no fields to update")
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
