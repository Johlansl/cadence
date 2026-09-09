"""Outbound webhook configuration.

The GET views are unauthenticated like the other dashboard read views, but the
stored URL is masked and the secret is never returned: the full URL and the
signing secret are shown once, at creation. Create / update / delete / test
need the X-Admin-Key. Deliveries are drained by the `scheduler` service.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi import status as http_status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.audit import record_audit
from app.api.deps import get_db, require_admin_key
from app.core.crypto import encrypt_token_secret
from app.models.models import Webhook, WebhookDelivery
from app.schemas.schemas import (
    WebhookCreate,
    WebhookCreated,
    WebhookOut,
    WebhookTestAccepted,
    WebhookUpdate,
)
from app.webhooks import mask_url
from app.webhooks.enqueue import enqueue_test

router = APIRouter(prefix="/api/v1", tags=["webhooks"])
admin_router = APIRouter(
    prefix="/api/v1/admin", tags=["webhooks"], dependencies=[Depends(require_admin_key)]
)


def _aggregates(db: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
    """Per-webhook delivery rollups for the list/detail views."""
    if not ids:
        return {}
    rows = db.execute(
        select(
            WebhookDelivery.webhook_id,
            func.max(WebhookDelivery.completed_at)
            .filter(WebhookDelivery.status == "delivered")
            .label("last_success_at"),
            func.count()
            .filter(WebhookDelivery.status == "pending")
            .label("pending"),
            func.count().filter(WebhookDelivery.status == "failed").label("failed"),
        )
        .where(WebhookDelivery.webhook_id.in_(ids))
        .group_by(WebhookDelivery.webhook_id)
    ).all()
    out: dict[uuid.UUID, dict] = {
        r.webhook_id: {
            "last_success_at": r.last_success_at,
            "last_error": None,
            "pending": r.pending,
            "failed": r.failed,
        }
        for r in rows
    }
    latest_error = db.execute(
        select(WebhookDelivery.webhook_id, WebhookDelivery.last_error)
        .distinct(WebhookDelivery.webhook_id)
        .where(
            WebhookDelivery.webhook_id.in_(ids),
            WebhookDelivery.last_error.is_not(None),
        )
        .order_by(WebhookDelivery.webhook_id, WebhookDelivery.created_at.desc())
    ).all()
    for webhook_id, err in latest_error:
        out.setdefault(
            webhook_id,
            {"last_success_at": None, "last_error": None, "pending": 0, "failed": 0},
        )["last_error"] = err
    return out


def _to_out(hook: Webhook, agg: dict | None) -> WebhookOut:
    agg = agg or {}
    return WebhookOut(
        id=hook.id,
        url_preview=mask_url(hook.url),
        enabled=hook.enabled,
        event_types=list(hook.event_types or []),
        description=hook.description,
        created_at=hook.created_at,
        updated_at=hook.updated_at,
        last_success_at=agg.get("last_success_at"),
        last_error=agg.get("last_error"),
        pending_count=agg.get("pending", 0),
        failed_count=agg.get("failed", 0),
    )


@router.get("/webhooks", response_model=list[WebhookOut])
def list_webhooks(db: Session = Depends(get_db)) -> list[WebhookOut]:
    hooks = (
        db.execute(select(Webhook).order_by(Webhook.created_at)).scalars().all()
    )
    agg = _aggregates(db, [h.id for h in hooks])
    return [_to_out(h, agg.get(h.id)) for h in hooks]


@router.get("/webhooks/{webhook_id}", response_model=WebhookOut)
def get_webhook(webhook_id: uuid.UUID, db: Session = Depends(get_db)) -> WebhookOut:
    hook = db.get(Webhook, webhook_id)
    if hook is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "webhook not found")
    return _to_out(hook, _aggregates(db, [hook.id]).get(hook.id))


@admin_router.post(
    "/webhooks", response_model=WebhookCreated, status_code=http_status.HTTP_201_CREATED
)
def create_webhook(
    request: Request, payload: WebhookCreate, db: Session = Depends(get_db)
) -> WebhookCreated:
    secret = secrets.token_urlsafe(32)
    hook = Webhook(
        url=payload.url,
        secret_encrypted=encrypt_token_secret(secret),
        enabled=payload.enabled,
        event_types=payload.event_types,
        description=payload.description,
    )
    db.add(hook)
    db.flush()  # populate hook.id for the audit row
    record_audit(
        db,
        request,
        "webhook.create",
        target_type="webhook",
        target_id=hook.id,
        detail={
            "url_preview": mask_url(hook.url),
            "event_types": hook.event_types,
            "enabled": hook.enabled,
        },
    )
    db.commit()
    db.refresh(hook)
    return WebhookCreated(
        id=hook.id,
        url=hook.url,
        secret=secret,
        enabled=hook.enabled,
        event_types=list(hook.event_types),
        description=hook.description,
        created_at=hook.created_at,
    )


@admin_router.patch("/webhooks/{webhook_id}", response_model=WebhookOut)
def update_webhook(
    request: Request,
    webhook_id: uuid.UUID,
    payload: WebhookUpdate,
    db: Session = Depends(get_db),
) -> WebhookOut:
    hook = db.get(Webhook, webhook_id)
    if hook is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "webhook not found")

    patch = payload.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(
            http_status.HTTP_422_UNPROCESSABLE_CONTENT, "no fields to update"
        )
    merged = {
        "url": hook.url,
        "event_types": list(hook.event_types or []),
        "enabled": hook.enabled,
        "description": hook.description,
        **patch,
    }
    try:
        validated = WebhookCreate(**merged)
    except ValidationError as exc:
        messages = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise HTTPException(
            http_status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"invalid webhook after update: {messages}",
        ) from exc
    for field, value in validated.model_dump().items():
        setattr(hook, field, value)
    hook.updated_at = datetime.now(timezone.utc)
    record_audit(
        db,
        request,
        "webhook.update",
        target_type="webhook",
        target_id=webhook_id,
        detail={"fields": sorted(patch)},
    )
    db.commit()
    db.refresh(hook)
    return _to_out(hook, _aggregates(db, [hook.id]).get(hook.id))


@admin_router.delete(
    "/webhooks/{webhook_id}", status_code=http_status.HTTP_204_NO_CONTENT
)
def delete_webhook(
    request: Request, webhook_id: uuid.UUID, db: Session = Depends(get_db)
) -> Response:
    hook = db.get(Webhook, webhook_id)
    if hook is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "webhook not found")
    db.delete(hook)
    record_audit(
        db,
        request,
        "webhook.delete",
        target_type="webhook",
        target_id=webhook_id,
        detail={"url_preview": mask_url(hook.url)},
    )
    db.commit()
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


@admin_router.post(
    "/webhooks/{webhook_id}/test",
    response_model=WebhookTestAccepted,
    status_code=http_status.HTTP_202_ACCEPTED,
)
def test_webhook(
    request: Request, webhook_id: uuid.UUID, db: Session = Depends(get_db)
) -> WebhookTestAccepted:
    hook = db.get(Webhook, webhook_id)
    if hook is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "webhook not found")
    # One real outbox row, delivered by the same dispatcher as a genuine event,
    # regardless of `enabled` / `event_types` (this endpoint is targeted).
    delivery_id = enqueue_test(db, hook)
    record_audit(
        db,
        request,
        "webhook.test",
        target_type="webhook",
        target_id=webhook_id,
        detail={"delivery_id": str(delivery_id)},
    )
    db.commit()
    return WebhookTestAccepted(delivery_id=delivery_id)
