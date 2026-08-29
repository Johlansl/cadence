"""Admin provisioning endpoints, guarded by the X-Admin-Key shared secret."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

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
def create_host(payload: HostCreate, db: Session = Depends(get_db)) -> HostCreated:
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
    db.commit()
    db.refresh(host)

    return HostCreated(id=host.id, hostname=host.hostname, token=token)


@router.patch("/hosts/{host_id}", response_model=HostPatched)
def update_host(
    host_id: uuid.UUID, payload: HostUpdate, db: Session = Depends(get_db)
) -> Host:
    host = db.get(Host, host_id)
    if host is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "host not found")
    host.reboot_policy = payload.reboot_policy
    host.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(host)
    return host


@router.post(
    "/hosts/{host_id}/jobs",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
)
def create_job(
    host_id: uuid.UUID, payload: JobCreate, db: Session = Depends(get_db)
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
    db.commit()
    db.refresh(job)
    return job
