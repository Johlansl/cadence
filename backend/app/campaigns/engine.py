"""The campaign engine: one pass per scheduler tick.

For each `running` campaign, in its own locked transaction:

  1. reconcile finished jobs into campaign_hosts state (a succeeded action is
     done only when health is healthy/degraded; unhealthy/unknown stops the
     rollout; failed actions skip or halt per CAMPAIGN_DISPOSITIONS). A
     succeeded upgrade the agent will reboot on stays running with its
     pre-reboot boot snapshotted until the host proves its return (a
     different boot plus fresh acceptable health); a reboot expected from
     an agent that sent no proof halts immediately; an unproven await past
     CADENCE_CAMPAIGN_RETURN_TIMEOUT_SECONDS halts;
  2. stop the campaign if a halt disposition fired, or if the skipped count
     has passed max_failures;
  3. otherwise create jobs for the active stage's not-yet-started hosts, up to
     the global max_concurrency (counting running campaign_hosts rows,
     reboot awaits included);
  4. once the active stage is fully terminal AND its observation window has
     elapsed, the next tick's "active stage" is naturally the next one; when
     there is no next stage, the campaign is completed.

Everything is derived from persisted rows, so a tick that dies part way
through simply rolls back and the next tick recomputes. `advance_campaigns`
never creates a job for a host that already has a pending/running one
(create_job_for_host returns None): it logs and retries next tick.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.campaigns.lifecycle import finalize_campaign_hosts
from app.core.config import settings
from app.db.base import SessionLocal
from app.job_creation import create_job_for_host
from app.models.models import Campaign, CampaignHost, Host, Job
from app.webhooks.enqueue import enqueue_event

log = logging.getLogger("cadence.campaigns")


def _f(**fields: object) -> dict:
    return {"fields": fields}


# Failed-job category -> what the campaign does about that host. "skip" drops
# the host from the rest of the campaign and counts toward max_failures; "halt"
# stops the whole campaign on the first occurrence. Lives here, not in the
# schema, so it can change without a migration. A category not in this map
# (an older agent's NULL, a future value) is treated as "halt" -- fail safe.
CAMPAIGN_DISPOSITIONS: dict[str, str] = {
    "apt_locked": "skip",
    "dpkg_error": "skip",
    "disk_full": "skip",
    "timeout": "skip",
    "agent_lost": "skip",
    "agent_refused": "skip",
    "network_or_repo": "halt",
    "unknown": "halt",
}

_ACTIVE = ("pending", "running")


class Outcome(NamedTuple):
    """What one campaign's advance did this tick -- logged, and translated into
    webhook events by _emit_events."""

    kind: str  # "waiting" | "filled" | "completed" | "stopped"
    stages_completed: tuple[int, ...] = ()
    stop_reason: str | None = None
    stop_category: str | None = None  # failure category or health halt category
    stop_host: str | None = None  # hostname, likewise
    jobs_created: int = 0


def _stop_campaign(db: Session, c: Campaign, *, reason: str, now: datetime) -> None:
    """Move a campaign to 'stopped' and terminal-ize every still-in-flight host
    in the same transaction (the engine never revisits a non-running campaign)."""
    c.status = "stopped"
    c.halt_reason = reason
    c.completed_at = now
    c.updated_at = now
    finalize_campaign_hosts(db, c.id, now=now)


def _stage_ready_at(
    rows: list[CampaignHost], jobs: dict[uuid.UUID, Job]
) -> datetime | None:
    """The latest completed_at among a stage's jobs -- when its observation
    window starts counting. None when the stage has no finished job to observe."""
    times = [
        jobs[r.job_id].completed_at
        for r in rows
        if r.job_id is not None
        and r.job_id in jobs
        and jobs[r.job_id].completed_at is not None
    ]
    return max(times) if times else None


def _active_stage(
    ch_rows: list[CampaignHost],
    jobs: dict[uuid.UUID, Job],
    c: Campaign,
    now: datetime,
) -> int | None:
    """The stage the engine may currently create jobs for, or None when every
    stage is terminal and past its observation window (campaign done). A stage
    that is fully terminal still "holds" the engine until its window elapses."""
    window = timedelta(seconds=c.observation_window_seconds)
    for stage in sorted({r.stage_index for r in ch_rows}):
        rows = [r for r in ch_rows if r.stage_index == stage]
        if any(r.state in _ACTIVE for r in rows):
            return stage  # still has work
        ready_at = _stage_ready_at(rows, jobs)
        if ready_at is not None and now < ready_at + window:
            return stage  # fully terminal, still inside the observation window
    return None


def _fresh_health(host: Host | None, ch: CampaignHost) -> str | None:
    """The host's health verdict when it postdates the await entry, else None.

    Freshness is measured against the row's updated_at (set when the await
    started), so the pre-reboot projection can never satisfy it. Both
    timestamps are server-side, so no clock comparison with the agent."""
    if (
        host is None
        or host.health_checked_at is None
        or host.health_checked_at <= ch.updated_at
    ):
        return None
    return host.health_status


def _return_proven(host: Host | None, ch: CampaignHost, fresh: str | None) -> bool:
    """True once the host reported back from a different boot with fresh
    acceptable health: the reboot happened and the host is back."""
    return (
        host is not None
        and host.current_boot_id is not None
        and host.current_boot_id != ch.awaited_boot
        and fresh in ("healthy", "degraded")
    )


def _reboot_expected(job: Job, host: Host | None, result: dict) -> bool:
    """True when the upgrade ran on auto with a pending reboot: the agent
    rebooted (old agents decide the same way) but sent no proof fields. The
    mode is the creation-time snapshot when present, else the live policy
    (rows created before pinning, same fallback the claim path uses)."""
    mode = (job.params or {}).get("reboot") or (host.reboot_policy if host else None)
    return mode == "auto" and bool(result.get("reboot_required"))


def _reconcile(
    db: Session,
    c: Campaign,
    ch_rows: list[CampaignHost],
    jobs: dict[uuid.UUID, Job],
    hosts: dict[uuid.UUID, Host],
    now: datetime,
) -> tuple[bool, list[int], str | None, str | None]:
    """Fold finished jobs into campaign_hosts state. Returns (halted,
    stages_that_became_fully_terminal_this_tick, halt_category, halt_host)."""
    completed_stages: list[int] = []
    for ch in ch_rows:
        if ch.state not in _ACTIVE:
            continue
        host = hosts.get(ch.host_id)
        hostname = host.hostname if host is not None else str(ch.host_id)

        if ch.awaited_boot is not None:
            # Awaiting proven return: the upgrade is done, only the host's
            # own post-reboot observation can close the row.
            fresh = _fresh_health(host, ch)
            if _return_proven(host, ch, fresh):
                ch.state = "done"
                ch.updated_at = now
            elif fresh is not None and fresh not in ("healthy", "degraded"):
                category = (
                    "health_unhealthy" if fresh == "unhealthy" else "health_unknown"
                )
                _stop_campaign(
                    db, c, reason=f"{category} on {hostname}", now=now
                )
                return True, completed_stages, category, hostname
            elif now - ch.updated_at > timedelta(
                seconds=settings.campaign_return_timeout_seconds
            ):
                _stop_campaign(
                    db, c, reason=f"return_timeout on {hostname}", now=now
                )
                return True, completed_stages, "return_timeout", hostname
            else:
                continue  # still waiting for the host to come back
        elif ch.job_id is None:
            if ch.state == "pending":
                continue  # job not created yet
            # Running without a job: the job row was deleted mid-flight (the
            # FK NULLs job_id on delete). Explicit halt, never a silent hang.
            _stop_campaign(db, c, reason=f"job_missing on {hostname}", now=now)
            return True, completed_stages, "job_missing", hostname
        else:
            job = jobs.get(ch.job_id)
            if job is None:
                _stop_campaign(db, c, reason=f"job_missing on {hostname}", now=now)
                return True, completed_stages, "job_missing", hostname
            if job.status not in ("succeeded", "failed"):
                continue  # still in flight

            if job.status == "succeeded":
                health = (job.result or {}).get("health_status")
                if health not in ("healthy", "degraded"):
                    category = (
                        "health_unhealthy" if health == "unhealthy" else "health_unknown"
                    )
                    _stop_campaign(
                        db, c, reason=f"{category} on {hostname}", now=now
                    )
                    return True, completed_stages, category, hostname
                result = job.result or {}
                will_reboot = result.get("will_reboot")
                if will_reboot is True:
                    pre_boot = result.get("boot_id")
                    if not pre_boot:
                        _stop_campaign(
                            db, c, reason=f"boot_proof_missing on {hostname}", now=now
                        )
                        return True, completed_stages, "boot_proof_missing", hostname
                    ch.awaited_boot = pre_boot
                    ch.updated_at = now
                    continue  # row stays running until proven return
                if will_reboot is None and _reboot_expected(job, host, result):
                    # Old agent on an auto host with a pending reboot: a reboot
                    # was attempted but no proof can ever arrive. Halt now with
                    # an actionable reason instead of waiting out a timeout.
                    _stop_campaign(
                        db, c, reason=f"boot_proof_missing on {hostname}", now=now
                    )
                    return True, completed_stages, "boot_proof_missing", hostname
                ch.state = "done"
            else:
                disp = CAMPAIGN_DISPOSITIONS.get(job.failure_category, "halt")
                if disp == "skip":
                    ch.state = "skipped"
                    ch.skip_reason = job.failure_category or "unknown"
                else:
                    category = job.failure_category or "unknown"
                    _stop_campaign(
                        db, c, reason=f"{category} on {hostname}", now=now
                    )
                    return True, completed_stages, category, hostname
            ch.updated_at = now

        stage_rows = [r for r in ch_rows if r.stage_index == ch.stage_index]
        if all(r.state not in _ACTIVE for r in stage_rows):
            completed_stages.append(ch.stage_index)

    return False, completed_stages, None, None


def _fill_stage(
    db: Session,
    c: Campaign,
    stage: int,
    ch_rows: list[CampaignHost],
    hostnames: dict[uuid.UUID, str],
    now: datetime,
) -> int:
    """Create jobs for `stage`'s not-yet-started hosts up to the global
    max_concurrency (running campaign_hosts rows, reboot awaits included).
    Returns how many were created. A host that already has an active job is
    left for the next tick."""
    # Counted from the already-loaded rows, not a fresh query, so the state
    # changes _reconcile just made in this transaction are visible.
    in_flight = sum(1 for r in ch_rows if r.state == "running")
    slots = c.max_concurrency - in_flight
    if slots <= 0:
        return 0

    pending = sorted(
        (r for r in ch_rows if r.stage_index == stage and r.state == "pending" and r.job_id is None),
        key=lambda r: hostnames.get(r.host_id, ""),
    )
    created = 0
    for ch in pending:
        if slots <= 0:
            break
        job = create_job_for_host(
            db, host_id=ch.host_id, campaign_id=c.id, requested_by="campaign"
        )
        if job is None:
            log.info(
                "campaign host has an active job, will retry",
                extra=_f(campaign_id=c.id, host_id=ch.host_id),
            )
            continue
        ch.job_id = job.id
        ch.state = "running"
        ch.updated_at = now
        slots -= 1
        created += 1
    return created


def _step(
    db: Session,
    c: Campaign,
    ch_rows: list[CampaignHost],
    jobs: dict[uuid.UUID, Job],
    hostnames: dict[uuid.UUID, str],
    hosts: dict[uuid.UUID, Host],
    now: datetime,
) -> Outcome:
    halted, completed_stages, halt_category, halt_host = _reconcile(
        db, c, ch_rows, jobs, hosts, now
    )
    if halted:
        return Outcome(
            "stopped",
            tuple(completed_stages),
            stop_reason=c.halt_reason,
            stop_category=halt_category,
            stop_host=halt_host,
        )

    skipped = sum(1 for r in ch_rows if r.state == "skipped")
    if skipped > c.max_failures:
        _stop_campaign(db, c, reason="max_failures exceeded", now=now)
        return Outcome(
            "stopped", tuple(completed_stages), stop_reason="max_failures exceeded"
        )

    stage = _active_stage(ch_rows, jobs, c, now)
    if stage is None:
        c.status = "completed"
        c.completed_at = now
        c.updated_at = now
        return Outcome("completed", tuple(completed_stages))

    created = _fill_stage(db, c, stage, ch_rows, hostnames, now)
    c.updated_at = now
    return Outcome(
        "filled" if created else "waiting", tuple(completed_stages), jobs_created=created
    )


def _counts(rows: list[CampaignHost]) -> dict:
    states = [r.state for r in rows]
    return {
        "hosts_total": len(states),
        "hosts_done": states.count("done"),
        "hosts_skipped": states.count("skipped"),
        "hosts_orphaned": states.count("orphaned"),
    }


def _emit_events(
    db: Session,
    c: Campaign,
    ch_rows: list[CampaignHost],
    outcome: Outcome,
    now: datetime,
) -> None:
    """Stage the webhook deliveries for this tick's transitions, in the same
    transaction as the state change (enqueue_event does not commit). Counts come
    from the post-mutation ch_rows, not a re-query (autoflush is off). Inert
    when webhooks are off or nothing subscribes."""
    base = {"campaign_id": str(c.id), "name": c.name}

    for idx in outcome.stages_completed:
        stage_rows = [r for r in ch_rows if r.stage_index == idx]
        enqueue_event(
            db,
            "campaign.stage_completed",
            {**base, "stage_index": idx, **_counts(stage_rows)},
            occurred_at=now,
        )

    if outcome.kind == "completed":
        enqueue_event(
            db,
            "campaign.completed",
            {**base, "status": "completed", **_counts(ch_rows)},
            occurred_at=now,
        )
    elif outcome.kind == "stopped":
        enqueue_event(
            db,
            "campaign.stopped",
            {
                **base,
                "status": "stopped",
                "reason": outcome.stop_reason,
                "halt_category": outcome.stop_category,
                "halt_host": outcome.stop_host,
                **_counts(ch_rows),
            },
            occurred_at=now,
        )


def _advance_one(db: Session, c: Campaign, now: datetime) -> Outcome:
    ch_rows = (
        db.execute(select(CampaignHost).where(CampaignHost.campaign_id == c.id))
        .scalars()
        .all()
    )
    if not ch_rows:  # a campaign with no hosts is trivially done
        c.status = "completed"
        c.completed_at = now
        c.updated_at = now
        return Outcome("completed")

    job_ids = [r.job_id for r in ch_rows if r.job_id is not None]
    jobs = {
        j.id: j
        for j in db.execute(select(Job).where(Job.id.in_(job_ids))).scalars()
    } if job_ids else {}
    host_ids = [r.host_id for r in ch_rows]
    hosts = (
        {
            h.id: h
            for h in db.execute(select(Host).where(Host.id.in_(host_ids))).scalars()
        }
        if host_ids
        else {}
    )
    hostnames = {hid: h.hostname for hid, h in hosts.items()}

    outcome = _step(db, c, ch_rows, jobs, hostnames, hosts, now)
    _emit_events(db, c, ch_rows, outcome, now)
    return outcome


def advance_campaigns(now: datetime | None = None, db: Session | None = None) -> int:
    """Advance every running campaign once. Returns the number advanced without
    error. Each campaign runs in its own SAVEPOINT then commit, so one bad
    campaign is rolled back in isolation and never blocks the others, and a
    mid-pass crash loses no committed work."""
    now = now or datetime.now(timezone.utc)
    own_session = db is None
    db = db or SessionLocal()
    advanced = 0
    try:
        ids = (
            db.execute(select(Campaign.id).where(Campaign.status == "running"))
            .scalars()
            .all()
        )
        for cid in ids:
            savepoint = db.begin_nested()  # rolled back alone if this campaign errors
            try:
                c = db.execute(
                    select(Campaign)
                    .where(Campaign.id == cid, Campaign.status == "running")
                    .with_for_update(skip_locked=True)
                ).scalar_one_or_none()
                if c is None:  # locked elsewhere, or no longer running
                    savepoint.rollback()
                    continue
                outcome = _advance_one(db, c, now)
                savepoint.commit()
                db.commit()
                advanced += 1
                log.info(
                    "campaign advanced",
                    extra=_f(
                        campaign_id=cid,
                        outcome=outcome.kind,
                        jobs_created=outcome.jobs_created,
                        stages_completed=list(outcome.stages_completed),
                        stop_reason=outcome.stop_reason,
                    ),
                )
            except Exception:  # noqa: BLE001 -- one campaign's failure is isolated
                savepoint.rollback()
                log.exception("campaign advance failed", extra=_f(campaign_id=cid))
    finally:
        if own_session:
            db.close()
    return advanced
