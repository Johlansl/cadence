"""Translate the events that happen in request handlers into webhook deliveries.

Called from the report and job-result handlers, on their own session, before
their existing commit, so a delivery row lands atomically with the change.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import Host, Job, WebhookHostState
from app.webhooks import iso_z, truncate_log
from app.webhooks.enqueue import enqueue_event


def on_job_result(
    db: Session,
    job: Job,
    host: Host,
    *,
    status: str,
    exit_code: int,
    log_text: str,
    occurred_at: datetime | None = None,
) -> None:
    """Enqueue job.succeeded / job.failed for a submitted job result."""
    if not settings.webhooks_enabled:
        return
    occurred_at = occurred_at or datetime.now(timezone.utc)
    failed = status == "failed"
    result = job.result or {}
    data = {
        "job_id": str(job.id),
        "host_id": str(job.host_id),
        "hostname": host.hostname,
        "job_type": job.job_type,
        "status": status,
        "exit_code": exit_code,
        "requested_by": job.requested_by,
        "completed_at": iso_z(job.completed_at) if job.completed_at else None,
        "log": truncate_log(log_text or "", settings.webhook_log_max_bytes),
        # None when not applicable (a reboot job, or an older agent); a
        # conflict can coincide with either outcome, so this is not gated on
        # `failed` the way the failure-classification keys below are.
        "held_conflicts": result.get("held_conflicts"),
        # Added in roadmap item 7 without new event names. All are null for a
        # non-upgrade job or an older agent, preserving one stable payload.
        "health_status": result.get("health_status"),
        "pre_checks": result.get("pre_checks"),
        "post_checks": result.get("post_checks"),
    }
    if failed:
        # Same three keys the reaper's job.failed carries (on_job_reaped), so a
        # consumer sees one shape for job.failed however the job failed.
        data["reaped"] = False
        data["failure_category"] = job.failure_category
        data["failure_summary"] = job.failure_summary
    enqueue_event(
        db,
        "job.failed" if failed else "job.succeeded",
        data,
        occurred_at=occurred_at,
    )


def on_job_reaped(
    db: Session,
    *,
    job_id: object,
    host_id: object,
    hostname: str,
    job_type: str,
    requested_by: str | None,
    failure_category: str | None,
    failure_summary: str | None,
    completed_at: datetime | None,
    log_text: str | None,
    occurred_at: datetime | None = None,
) -> None:
    """Enqueue job.failed for a job the scheduler reaper failed. Same payload
    as on_job_result's failed branch, plus `reaped: true` and a null
    `exit_code` (a reaped job ran no process to completion). Takes plain values,
    not ORM objects: the reaper updates in bulk and never loads the rows."""
    if not settings.webhooks_enabled:
        return
    occurred_at = occurred_at or datetime.now(timezone.utc)
    enqueue_event(
        db,
        "job.failed",
        {
            "job_id": str(job_id),
            "host_id": str(host_id),
            "hostname": hostname,
            "job_type": job_type,
            "status": "failed",
            "exit_code": None,
            "reaped": True,
            "requested_by": requested_by,
            "completed_at": iso_z(completed_at) if completed_at else None,
            "log": truncate_log(log_text or "", settings.webhook_log_max_bytes),
            "failure_category": failure_category,
            "failure_summary": failure_summary,
            "health_status": None,
            "pre_checks": None,
            "post_checks": None,
        },
        occurred_at=occurred_at,
    )


def on_report(
    db: Session,
    host: Host,
    *,
    was_reboot_required: bool,
    now_reboot_required: bool,
    security_updates: int,
    occurred_at: datetime | None = None,
) -> None:
    """From one accepted agent report: reset the offline flag (the host is
    alive), enqueue host.reboot_required on a false->true edge, and enqueue
    host.security_updates_available when the security count is non-zero and has
    changed since the last notification sent for this host."""
    if not settings.webhooks_enabled:
        return
    occurred_at = occurred_at or datetime.now(timezone.utc)
    state = db.get(WebhookHostState, host.id)

    if state is not None and state.offline_notified:
        state.offline_notified = False
        state.updated_at = occurred_at

    if now_reboot_required and not was_reboot_required:
        enqueue_event(
            db,
            "host.reboot_required",
            {
                "host_id": str(host.id),
                "hostname": host.hostname,
                "reboot_required": True,
            },
            occurred_at=occurred_at,
        )

    last = state.security_updates_notified if state is not None else None
    if security_updates > 0 and security_updates != last:
        enqueue_event(
            db,
            "host.security_updates_available",
            {
                "host_id": str(host.id),
                "hostname": host.hostname,
                "security_updates_count": security_updates,
                "previous_count": last,
            },
            occurred_at=occurred_at,
        )
        if state is None:
            state = WebhookHostState(host_id=host.id, offline_notified=False)
            db.add(state)
        state.security_updates_notified = security_updates
        state.updated_at = occurred_at
