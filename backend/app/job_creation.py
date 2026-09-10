"""The one path every job goes through.

Job rows are created from three places: the admin route
(`POST /admin/hosts/{id}/jobs`), the scheduler turning a due schedule into a
job, and the campaign engine filling a stage. They must all inject the
server-resolved held set the same way, so a job's `excluded_packages` /
`known_held_packages` can never drift from current policy. This module is that
shared path; `app.exclusions` resolves the rules, callers own the commit and
the audit row.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exclusions import known_held_for_host, resolve_for_job
from app.models.models import Job


def create_job_for_host(
    db: Session,
    *,
    host_id: uuid.UUID,
    job_type: str = "apt_upgrade",
    params: dict | None = None,
    requested_by: str | None = None,
    campaign_id: uuid.UUID | None = None,
) -> Job | None:
    """Create one job for `host_id`, or return None if the host already has a
    pending or running job.

    The None case is the "one active job per host" rule; each caller decides
    what it means -- the admin route turns it into 409, the scheduler and the
    campaign engine log it and retry on their next pass.

    For an `apt_upgrade` or `apt_dry_run` job the server-resolved
    `params.excluded_packages` is injected, and for `apt_upgrade` also
    `params.known_held_packages`; both always overwrite whatever the caller
    passed under those keys. The Job is added and flushed (so `job.id` is
    populated) but not committed, and no audit row is written -- the caller
    owns both. The host is assumed to exist (FK-guaranteed for the scheduler
    and engine callers; the route checks it first).
    """
    active = db.execute(
        select(Job.id)
        .where(Job.host_id == host_id, Job.status.in_(("pending", "running")))
        .limit(1)
    ).first()
    if active is not None:
        return None

    params = dict(params or {})
    if job_type in ("apt_upgrade", "apt_dry_run"):
        params["excluded_packages"] = resolve_for_job(db, host_id)
    if job_type == "apt_upgrade":
        params["known_held_packages"] = known_held_for_host(db, host_id)

    job = Job(
        host_id=host_id,
        job_type=job_type,
        params=params,
        requested_by=requested_by,
        campaign_id=campaign_id,
    )
    db.add(job)
    db.flush()  # populate job.id
    return job
