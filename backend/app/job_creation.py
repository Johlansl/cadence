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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.exclusions import known_held_for_host, resolve_for_job
from app.models.models import Job

ACTIVE_JOB_INDEX = "ux_jobs_one_active_per_host"


def _is_active_job_collision(exc: IntegrityError) -> bool:
    """True only for our own one-active-job index.

    A bare pgcode check would also swallow a different unique violation on
    the same flush (e.g. a jobs_pkey clash from a client-supplied id).
    PostgreSQL names the violated index in the error diagnostic, so require
    the exact name; anything else propagates.
    """
    orig = getattr(exc, "orig", None)
    if getattr(orig, "pgcode", None) != "23505":
        return False
    # psycopg2 (the pinned driver) always names the violated index here
    # (proven by test_direct_insert names it); accept only ours. Anything
    # unnamed or otherwise named propagates instead of reading as "busy".
    return getattr(getattr(orig, "diag", None), "constraint_name", None) == ACTIVE_JOB_INDEX


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
    `params.excluded_packages` is injected. `apt_upgrade` additionally gets
    `params.known_held_packages`. Both `apt_upgrade` and `health_check` get
    the server's health-check thresholds in `params.health_checks`, since
    `health_check` reruns the same disk-space checks under the same policy.
    All of these always overwrite whatever the caller passed under those
    keys. The Job is added and flushed (so `job.id` is populated) but not
    committed, and no audit row is written -- the caller owns both. The host
    is assumed to exist (FK-guaranteed for the scheduler and engine callers;
    the route checks it first).
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
    if job_type in ("apt_upgrade", "health_check"):
        params["health_checks"] = {
            "minimum_available_bytes": settings.upgrade_minimum_available_bytes,
            "boot_minimum_available_bytes": settings.upgrade_boot_minimum_available_bytes,
            "lock_wait_seconds": settings.upgrade_lock_wait_seconds,
        }

    job = Job(
        host_id=host_id,
        job_type=job_type,
        params=params,
        requested_by=requested_by,
        campaign_id=campaign_id,
    )
    # A concurrent creator may commit an active job for this host between the
    # check above and this flush. The savepoint turns that lost race into the
    # same None ("host busy") the check returns. Explicit begin/rollback (not
    # a `with` block) so session event listeners cannot close the transaction
    # under us. Proven by instrumentation (autoflush False, traced flushes):
    # begin_nested() first flushes the caller's pending work into the outer
    # transaction, then the helper's own flush writes only the INSERT inside
    # the savepoint. Rolling back therefore undoes the INSERT alone; the
    # caller work is already safe in the outer transaction (or still dirty
    # in memory) and commits with it. The failed row is detached by the
    # rollback, so a later flush cannot retry it.
    savepoint = db.begin_nested()
    try:
        db.add(job)
        db.flush()  # populate job.id
    except IntegrityError as exc:
        savepoint.rollback()
        # Only our own index maps to "busy"; any other integrity failure
        # (e.g. a host deleted mid-flight) keeps propagating.
        if not _is_active_job_collision(exc):
            raise
        return None
    else:
        savepoint.commit()
    return job
