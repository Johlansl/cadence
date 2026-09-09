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
    event_type = "job.succeeded" if status == "succeeded" else "job.failed"
    enqueue_event(
        db,
        event_type,
        {
            "job_id": str(job.id),
            "host_id": str(job.host_id),
            "hostname": host.hostname,
            "job_type": job.job_type,
            "status": status,
            "exit_code": exit_code,
            "requested_by": job.requested_by,
            "completed_at": iso_z(job.completed_at) if job.completed_at else None,
            "log": truncate_log(log_text or "", settings.webhook_log_max_bytes),
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
