"""The one path every job goes through.

Job rows are created from three places: the admin route
(`POST /admin/hosts/{id}/jobs`), the scheduler turning a due schedule into a
job, and the campaign engine filling a stage. They must all inject the
server-resolved held set the same way, so a job's `excluded_packages` /
`known_held_packages` can never drift from current policy. This module is that
shared path; `app.exclusions` resolves the rules, callers own the commit and
the audit row. It also enforces the global reboot budget (6A) when enabled.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import and_, func, literal, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.exclusions import known_held_for_host, resolve_for_job
from app.models.models import CampaignHost, Host, Job

log = logging.getLogger("cadence.jobs")

ACTIVE_JOB_INDEX = "ux_jobs_one_active_per_host"

# Advisory-lock key serializing reboot-budget admission. Fixed and arbitrary
# (any 64-bit value PostgreSQL never sees elsewhere); the check+insert below
# is its only user.
_REBOOT_BUDGET_LOCK_KEY = 8102746130295173041


def _f(**fields: object) -> dict:
    return {"fields": fields}


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


def unproven_reboot_hold_clause():
    """WHERE clause matching terminal jobs whose reboot is still unproven.

    Shared by the budget count and the retention sweep, so the two can never
    disagree: the sweep must not delete a row this clause matches, or the
    budget would silently release a hold nobody proved. Host references use
    EXISTS subqueries so the clause works both in a SELECT and in a DELETE
    (which cannot join); callers must not add their own Host join for it.
    """
    boot = func.nullif(Job.result["boot_id"].astext, "")
    will = Job.result["will_reboot"].astext
    pinned_auto = Job.params["reboot"].astext == "auto"
    host_live_auto = (
        select(literal(1))
        .select_from(Host)
        .where(Host.id == Job.host_id, Host.reboot_policy == "auto")
        .exists()
    )
    boot_unproven = (
        select(literal(1))
        .select_from(Host)
        .where(
            Host.id == Job.host_id,
            or_(
                boot.is_(None),
                Host.current_boot_id.is_(None),
                Host.current_boot_id == boot,
            ),
        )
        .exists()
    )
    # Missing keys compare NULL, and a NULL under the retention's NOT would
    # spare the row: coalesce to FALSE so "no reboot evidence" never holds.
    attempted = func.coalesce(
        or_(
            and_(
                Job.job_type == "apt_upgrade",
                or_(
                    will == "true",
                    and_(
                        will.is_(None),
                        or_(
                            pinned_auto,
                            and_(
                                Job.params["reboot"].astext.is_(None),
                                host_live_auto,
                            ),
                        ),
                        Job.result["reboot_required"].astext == "true",
                    ),
                ),
            ),
            and_(
                Job.job_type == "reboot",
                or_(will == "true", will.is_(None)),
            ),
        ),
        False,
    )
    return and_(
        Job.status == "succeeded",
        attempted,
        boot_unproven,
    )


def _consumes_reboot_budget(job_type: str, params: dict) -> bool:
    """True when creating this job may lead to a reboot. Dedicated reboot jobs
    always count; apt_upgrade counts only on auto (the creation-time pinned
    mode -- never/prompt never reboot via the agent). Read-only types
    (health_check, apt_dry_run) never do."""
    if job_type == "reboot":
        return True
    if job_type == "apt_upgrade":
        return params.get("reboot") == "auto"
    return False


def _reboot_budget_in_use(db: Session) -> int:
    """Distinct hosts with an unproven reboot, across all origins.

    A reboot stays counted without interruption from admission until the host
    is observed back on a new boot: active reboot-capable jobs, then terminal
    jobs whose reboot is unproven (the pre-reboot boot is still current),
    then campaign awaits (whose snapshot survives job deletion). The hold
    never expires on its own: a timeout may halt a campaign's progression,
    but only an observed new boot (or an explicit operator recovery such as
    deleting the job history) releases the capacity. Read-only types never
    count. No new state: everything derives from the job result, the host's
    observed boot and the await snapshot."""
    auto_mode = or_(
        Job.params["reboot"].astext == "auto",
        and_(
            Job.params["reboot"].astext.is_(None),
            Host.reboot_policy == "auto",
        ),
    )
    active = set(
        db.execute(
            select(Job.host_id)
            .select_from(Job)
            .join(Host, Host.id == Job.host_id)
            .where(
                Job.status.in_(("pending", "running")),
                or_(
                    Job.job_type == "reboot",
                    and_(Job.job_type == "apt_upgrade", auto_mode),
                ),
            )
        )
        .scalars()
        .all()
    )
    unproven = set(
        db.execute(select(Job.host_id).where(unproven_reboot_hold_clause()))
        .scalars()
        .all()
    )
    awaited = set(
        db.execute(
            select(CampaignHost.host_id).where(
                CampaignHost.state == "running",
                CampaignHost.awaited_boot.is_not(None),
            )
        )
        .scalars()
        .all()
    )
    return len(active | unproven | awaited)


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
    pending or running job, or if the global reboot budget is exhausted.

    The None case is the "one active job per host" rule plus the reboot
    budget; each caller decides what it means -- the admin route turns it
    into 409, the scheduler and the campaign engine log it and retry on
    their next pass. Read-only types (health_check, apt_dry_run) never
    consume the budget.

    For an `apt_upgrade` or `apt_dry_run` job the server-resolved
    `params.excluded_packages` is injected. `apt_upgrade` additionally gets
    `params.known_held_packages` and, unless the caller passed an explicit
    `reboot` override, a snapshot of the host's current `reboot_policy`.
    Both `apt_upgrade` and `health_check` get
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
    if job_type == "apt_upgrade" and "reboot" not in params:
        # Snapshot the host's current policy so the reboot decision, the claim
        # path and the future reboot budget all read the same value. An
        # explicit caller override keeps precedence. A later policy change no
        # longer affects this job; rows created before this pinning still fall
        # back to the live policy at claim time.
        policy = db.execute(
            select(Host.reboot_policy).where(Host.id == host_id)
        ).scalar_one_or_none()
        if policy is not None:
            params["reboot"] = policy
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

    budget = settings.max_concurrent_reboots
    if budget > 0 and _consumes_reboot_budget(job_type, params):
        # Serialize check+admission across every concurrent creator (admin
        # requests, scheduler, engine): without the lock two transactions
        # could both read under the cap and both insert. Transaction-scoped,
        # so no state survives a crash; skipped entirely when the budget is
        # off, leaving historic behavior untouched.
        db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _REBOOT_BUDGET_LOCK_KEY},
        )
        if _reboot_budget_in_use(db) >= budget:
            log.info(
                "reboot budget exhausted, job refused",
                extra=_f(host_id=host_id, job_type=job_type),
            )
            return None

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
