"""Write webhook delivery rows into the outbox.

`enqueue_event` runs on the caller's session (a request handler or a scheduler
task) and is committed by that caller, so the delivery row lands atomically
with the change that produced the event.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import Webhook, WebhookDelivery
from app.webhooks import TEST_EVENT_TYPE, event_body


def enqueue_event(
    db: Session,
    event_type: str,
    data: dict,
    *,
    occurred_at: datetime | None = None,
) -> int:
    """Stage one delivery row per enabled webhook subscribed to `event_type`.
    No-op (returns 0) when webhooks are disabled globally. Does not commit."""
    if not settings.webhooks_enabled:
        return 0
    occurred_at = occurred_at or datetime.now(timezone.utc)
    hooks = (
        db.execute(select(Webhook).where(Webhook.enabled.is_(True))).scalars().all()
    )
    staged = 0
    for hook in hooks:
        if event_type not in (hook.event_types or []):
            continue
        delivery_id = uuid.uuid4()
        db.add(
            WebhookDelivery(
                id=delivery_id,
                webhook_id=hook.id,
                event_type=event_type,
                payload=event_body(
                    event_type, data, occurred_at=occurred_at, delivery_id=delivery_id
                ),
                status="pending",
                attempt_count=0,
                next_attempt_at=occurred_at,
            )
        )
        staged += 1
    return staged


def enqueue_test(db: Session, webhook: Webhook) -> uuid.UUID:
    """Stage a single `webhook.test` delivery for one explicitly targeted
    webhook, regardless of its `enabled` flag or its `event_types`. Does not
    commit. Returns the new delivery id."""
    now = datetime.now(timezone.utc)
    delivery_id = uuid.uuid4()
    data = {"message": "Test delivery from Cadence.", "webhook_id": str(webhook.id)}
    db.add(
        WebhookDelivery(
            id=delivery_id,
            webhook_id=webhook.id,
            event_type=TEST_EVENT_TYPE,
            payload=event_body(
                TEST_EVENT_TYPE, data, occurred_at=now, delivery_id=delivery_id
            ),
            status="pending",
            attempt_count=0,
            next_attempt_at=now,
        )
    )
    return delivery_id
