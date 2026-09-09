"""Periodic host.offline detector, run on the scheduler loop.

A host whose last_seen_at is older than CADENCE_WEBHOOK_OFFLINE_AFTER_SECONDS
gets one host.offline event. The "notify once" guarantee is the
webhook_host_state.offline_notified flag, cleared in the report handler when the
host comes back, not a timer, so this can run every tick.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import SessionLocal
from app.models.models import Host, WebhookHostState
from app.webhooks import _f, iso_z
from app.webhooks.enqueue import enqueue_event

log = logging.getLogger("cadence.webhooks")


def scan_offline_hosts(
    now: datetime | None = None, db: Session | None = None
) -> int:
    """Enqueue host.offline for every active host that has gone quiet and has
    not already been flagged. Returns the number of hosts newly flagged offline
    this pass. Pass `db` to run inside an existing session (tests)."""
    if not settings.webhooks_enabled:
        return 0
    now = now or datetime.now(timezone.utc)
    own_session = db is None
    db = db or SessionLocal()
    flagged = 0
    try:
        cutoff = now - timedelta(seconds=settings.webhook_offline_after_seconds)
        hosts = (
            db.execute(
                select(Host).where(
                    Host.is_active.is_(True),
                    Host.last_seen_at.is_not(None),
                    Host.last_seen_at < cutoff,
                )
            )
            .scalars()
            .all()
        )
        for host in hosts:
            state = db.get(WebhookHostState, host.id)
            if state is None:
                state = WebhookHostState(host_id=host.id, offline_notified=False)
                db.add(state)
            if state.offline_notified:
                continue
            enqueue_event(
                db,
                "host.offline",
                {
                    "host_id": str(host.id),
                    "hostname": host.hostname,
                    "last_seen_at": iso_z(host.last_seen_at),
                    "threshold_seconds": settings.webhook_offline_after_seconds,
                },
                occurred_at=now,
            )
            state.offline_notified = True
            state.updated_at = now
            flagged += 1
        db.commit()
    finally:
        if own_session:
            db.close()
    if flagged:
        log.info("hosts marked offline", extra=_f(count=flagged))
    return flagged
