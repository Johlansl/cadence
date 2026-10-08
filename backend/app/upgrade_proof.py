"""Upgrade proof (6B): completing an `agent_upgrade` job from an observed version.

An `agent_upgrade` is never succeeded on the word of the process that
replaces the binary. Success needs a later authenticated contact (poll or
report) carrying `agent_version == params.target_version`. Both contact
paths call `complete_agent_upgrade_if_proven`; the sweeper in
`app.scheduler` fails upgrades that never prove in time.

No process memory: every decision re-reads PostgreSQL, so a restart loses
nothing. No new job status, no intermediate result.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.models import Host, Job
from app.webhooks.events import on_job_result

log = logging.getLogger("cadence.upgrade_proof")


def _f(**fields: object) -> dict:
    """Wrap structured fields for the logfmt formatter."""
    return {"fields": fields}


def complete_agent_upgrade_if_proven(
    db: Session, host: Host, agent_version: str | None, now: datetime
) -> Job | None:
    """Succeed the host's running `agent_upgrade` iff `agent_version` exactly
    equals its `target_version`. Returns the completed job, else None.

    The transition is atomic (UPDATE ... WHERE status='running' RETURNING,
    the same winner election as the result callback): concurrent proofs, a
    concurrent agent result, or the proof-timeout sweeper race on the row
    and exactly one of them moves it to terminal. The loser finds zero rows
    and stages nothing, so there is exactly one terminal event.

    Never commits: the caller owns the transaction (poll, report). Never
    raises on a version mismatch: a wrong or absent version leaves the job
    running and the timeout decides.
    """
    if not agent_version:
        return None
    job = db.execute(
        select(Job).where(
            Job.host_id == host.id,
            Job.job_type == "agent_upgrade",
            Job.status == "running",
        )
    ).scalar_one_or_none()
    if job is None:
        return None
    target = job.params.get("target_version")
    if not target or agent_version != target:
        return None
    won = (
        db.execute(
            update(Job)
            .where(
                Job.id == job.id,
                Job.host_id == host.id,
                Job.status == "running",
            )
            .values(
                status="succeeded",
                completed_at=now,
                result={"proven": True},
                log=(
                    f"[cadence] agent_version {agent_version} matched "
                    "target_version; marked succeeded by upgrade proof"
                ),
            )
            .returning(Job.id)
        ).scalar_one_or_none()
        is not None
    )
    if not won:
        return None
    # Bypass the identity map: the job object loaded above still shows the
    # pre-race row. Refresh it so the webhook below reads the stored values.
    db.refresh(job)
    log.info(
        "agent upgrade proven",
        extra=_f(job_id=str(job.id), host_id=str(host.id), agent_version=agent_version),
    )
    # Normal terminal event: the same job.succeeded the result callback
    # emits. No agent process produced an exit code; 0 is the neutral value.
    on_job_result(
        db,
        job,
        host,
        status="succeeded",
        exit_code=0,
        log_text=job.log or "",
        occurred_at=job.completed_at,
    )
    return job
