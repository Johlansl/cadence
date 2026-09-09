"""Job views and the agent's result callback.

Job creation lives in admin.py (X-Admin-Key). The GET endpoints are
unauthenticated like the other dashboard read views; the result callback is
authenticated by the host's own token.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as http_status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_host, get_db
from app.api.pagination import before_keyset
from app.models.models import Host, Job
from app.schemas.schemas import JobHandoff, JobOut, JobResultIn, NextJob
from app.webhooks.events import on_job_result

router = APIRouter(prefix="/api/v1", tags=["jobs"])


def claim_pending_job(db: Session, host: Host, now) -> Job | None:
    """Take the oldest pending job for the host and mark it running. Shared by
    the report piggyback and the /agent/next-job poll. SKIP LOCKED + the status
    filter make it safe if both fire at once.

    Resolves the effective reboot decision now and pins it into job.params, so
    the agent gets a concrete 'auto'/'never' and the job record shows what was
    decided: a per-job params.reboot override wins, else the host's
    reboot_policy.
    """
    job = db.execute(
        select(Job)
        .where(Job.host_id == host.id, Job.status == "pending")
        .order_by(Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if job is not None:
        job.status = "running"
        job.started_at = now
        if job.job_type == "apt_upgrade":
            effective_reboot = job.params.get("reboot") or host.reboot_policy
            job.params = {**job.params, "reboot": effective_reboot}
    return job


@router.post("/agent/next-job", response_model=NextJob)
def claim_next_job(
    host: Host = Depends(get_current_host), db: Session = Depends(get_db)
) -> NextJob:
    now = datetime.now(timezone.utc)
    job = claim_pending_job(db, host, now)
    host.last_seen_at = now  # a poll is also a liveness signal
    handoff = (
        JobHandoff(id=job.id, job_type=job.job_type, params=job.params)
        if job is not None
        else None
    )
    db.commit()
    return NextJob(job=handoff)


@router.get("/hosts/{host_id}/jobs", response_model=list[JobOut])
def list_host_jobs(
    host_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=200),
    before: datetime | None = Query(
        None, description="page cursor: created_at of the last row you have"
    ),
    before_id: uuid.UUID | None = Query(
        None, description="page cursor: id of the last row you have (pass with `before`)"
    ),
    db: Session = Depends(get_db),
) -> list[Job]:
    if db.get(Host, host_id) is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "host not found")
    stmt = select(Job).where(Job.host_id == host_id)
    keyset = before_keyset(Job.created_at, Job.id, before, before_id)
    if keyset is not None:
        stmt = stmt.where(keyset)
    stmt = stmt.order_by(Job.created_at.desc(), Job.id.desc()).limit(limit)
    return list(db.execute(stmt).scalars().all())


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
    result: dict = {
        "exit_code": payload.exit_code,
        "reboot_required": payload.reboot_required,
    }
    # held_conflicts (roadmap item 3): only present when the agent actually
    # reconciled holds this run (an apt_upgrade job on agent >= 0.10.0), on
    # either outcome. Absent from result entirely otherwise, so a None here
    # is distinguishable from "checked, found nothing" ([]).
    if payload.held_conflicts is not None:
        result["held_conflicts"] = payload.held_conflicts
    job.result = result
    # Failure classification (roadmap item 2). Only meaningful for a failed job;
    # ignore whatever the agent sent on success. The agent already caps the
    # summary, clip defensively in case it does not.
    if payload.status == "failed":
        job.failure_category = payload.failure_category
        summary = payload.failure_summary
        job.failure_summary = summary[:500] if summary else None
    else:
        job.failure_category = None
        job.failure_summary = None
    job.completed_at = datetime.now(timezone.utc)

    # A completed reboot job clears the host's reboot-required flag right away
    # (the /run/reboot-required file is gone after the reboot). If the reboot
    # somehow did not happen, the next report re-sets it.
    if job.job_type == "reboot" and payload.status == "succeeded":
        host.reboot_required = False
        host.updated_at = job.completed_at

    on_job_result(
        db,
        job,
        host,
        status=payload.status,
        exit_code=payload.exit_code,
        log_text=payload.log,
        occurred_at=job.completed_at,
    )

    db.commit()
    db.refresh(job)
    return job
