"""Job views and the agent's result callback.

Job creation lives in admin.py (X-Admin-Key). The GET endpoints are
unauthenticated like the other dashboard read views; the result callback is
authenticated by the host's own token.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_host, get_db
from app.models.models import Host, Job
from app.schemas.schemas import JobHandoff, JobOut, JobResultIn, NextJob

router = APIRouter(prefix="/api/v1", tags=["jobs"])


def claim_pending_job(db: Session, host_id: uuid.UUID, now) -> Job | None:
    """Take the oldest pending job for the host and mark it running. Shared by
    the report piggyback and the /agent/next-job poll. SKIP LOCKED + the status
    filter make it safe if both fire at once."""
    job = db.execute(
        select(Job)
        .where(Job.host_id == host_id, Job.status == "pending")
        .order_by(Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if job is not None:
        job.status = "running"
        job.started_at = now
    return job


@router.post("/agent/next-job", response_model=NextJob)
def claim_next_job(
    host: Host = Depends(get_current_host), db: Session = Depends(get_db)
) -> NextJob:
    now = datetime.now(timezone.utc)
    job = claim_pending_job(db, host.id, now)
    host.last_seen_at = now  # a poll is also a liveness signal
    handoff = (
        JobHandoff(id=job.id, job_type=job.job_type, params=job.params)
        if job is not None
        else None
    )
    db.commit()
    return NextJob(job=handoff)


@router.get("/hosts/{host_id}/jobs", response_model=list[JobOut])
def list_host_jobs(host_id: uuid.UUID, db: Session = Depends(get_db)) -> list[Job]:
    if db.get(Host, host_id) is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "host not found")
    return list(
        db.execute(
            select(Job)
            .where(Job.host_id == host_id)
            .order_by(Job.created_at.desc())
            .limit(20)
        )
        .scalars()
        .all()
    )


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db)) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "job not found")
    return job


@router.post("/jobs/{job_id}/result", response_model=JobOut)
def submit_job_result(
    job_id: uuid.UUID,
    payload: JobResultIn,
    host: Host = Depends(get_current_host),
    db: Session = Depends(get_db),
) -> Job:
    job = db.get(Job, job_id)
    # Same 404 whether the job is missing or belongs to another host.
    if job is None or job.host_id != host.id:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "job not found")
    if job.status != "running":
        raise HTTPException(
            http_status.HTTP_409_CONFLICT,
            f"job is not running (status={job.status})",
        )

    job.status = payload.status
    job.log = payload.log
    job.result = {
        "exit_code": payload.exit_code,
        "reboot_required": payload.reboot_required,
    }
    job.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(job)
    return job
