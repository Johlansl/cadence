"""Sign and POST pending webhook deliveries, with retry and exponential
backoff. Driven by the scheduler loop (app.scheduler)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import decrypt_token_secret
from app.db.base import SessionLocal
from app.models.models import Webhook, WebhookDelivery
from app.webhooks import _f

log = logging.getLogger("cadence.webhooks")

_BACKOFF_BASE_SECONDS = 60
_BACKOFF_CAP_SECONDS = 3600
_ERROR_MAX_CHARS = 2000


def backoff_delay(attempt_count: int) -> timedelta:
    """Wait before the next attempt after `attempt_count` failed ones:
    60s, 120s, 240s, ... capped at 1 hour."""
    exp = min(max(attempt_count - 1, 0), 20)
    seconds = min(_BACKOFF_BASE_SECONDS * (2**exp), _BACKOFF_CAP_SECONDS)
    return timedelta(seconds=seconds)


def sign_body(secret: str, send_ts: str, body_bytes: bytes) -> str:
    """HMAC-SHA256 over "{send_ts}\\n{sha256_hex(body)}", keyed with the webhook
    secret. The agent request-signing construction (app.api.deps._auth_signed)
    reduced to the two parts an arbitrary receiver can reproduce."""
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    canonical = f"{send_ts}\n{body_hash}"
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def _post(url: str, body_bytes: bytes, headers: dict[str, str], timeout: float) -> int:
    """POST and return the HTTP status. Raises urllib.error.HTTPError for
    >= 400 and urllib.error.URLError / OSError for a transport failure. Follows
    3xx redirects (the urllib default); a redirecting webhook URL is a
    misconfiguration, documented in SECURITY.md."""
    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read(4096)
        return int(resp.status)


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"{type(exc).__name__}: {exc.reason}"
    return f"{type(exc).__name__}: {exc}"


def _record_failure(delivery: WebhookDelivery, now: datetime, message: str) -> None:
    delivery.last_error = message[:_ERROR_MAX_CHARS]
    if delivery.attempt_count >= settings.webhook_max_attempts:
        delivery.status = "failed"
        delivery.completed_at = now
        log.warning(
            "webhook delivery failed permanently",
            extra=_f(
                delivery_id=str(delivery.id),
                webhook_id=str(delivery.webhook_id),
                attempts=delivery.attempt_count,
                error=message[:200],
            ),
        )
    else:
        delivery.next_attempt_at = now + backoff_delay(delivery.attempt_count)
        log.info(
            "webhook delivery attempt failed, will retry",
            extra=_f(
                delivery_id=str(delivery.id),
                webhook_id=str(delivery.webhook_id),
                attempt=delivery.attempt_count,
                next_attempt_at=delivery.next_attempt_at.isoformat(),
                error=message[:200],
            ),
        )


def attempt_delivery(
    delivery: WebhookDelivery, url: str, secret_encrypted: str, now: datetime
) -> None:
    """One delivery attempt. Mutates `delivery` in place (status / attempt_count
    / next_attempt_at / last_error / completed_at); the caller commits."""
    delivery.attempt_count += 1

    secret = decrypt_token_secret(secret_encrypted)
    if secret is None:
        _record_failure(
            delivery, now, "webhook secret cannot be decrypted (key rotated away?)"
        )
        return

    body_bytes = json.dumps(
        delivery.payload, separators=(",", ":"), sort_keys=True
    ).encode()
    send_ts = str(int(now.timestamp()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "cadence-webhook",
        "X-Cadence-Event": delivery.event_type,
        "X-Cadence-Delivery": str(delivery.id),
        "X-Cadence-Timestamp": send_ts,
        "X-Cadence-Signature": sign_body(secret, send_ts, body_bytes),
    }

    try:
        status = _post(url, body_bytes, headers, settings.webhook_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 -- any failure is a retryable delivery failure
        _record_failure(delivery, now, _error_text(exc))
        return

    if not 200 <= status < 300:
        _record_failure(delivery, now, f"HTTP {status}")
        return

    delivery.status = "delivered"
    delivery.completed_at = now
    delivery.last_error = None


def dispatch_pending_deliveries(
    now: datetime | None = None, db: Session | None = None
) -> int:
    """Drain up to `webhook_dispatch_batch` ready pending deliveries. Returns
    the number delivered this pass. Pass `db` to run inside an existing session
    (tests)."""
    if not settings.webhooks_enabled:
        return 0
    now = now or datetime.now(timezone.utc)
    own_session = db is None
    db = db or SessionLocal()
    delivered = 0
    picked = 0
    try:
        rows = db.execute(
            select(WebhookDelivery, Webhook.url, Webhook.secret_encrypted)
            .join(Webhook, Webhook.id == WebhookDelivery.webhook_id)
            .where(
                WebhookDelivery.status == "pending",
                WebhookDelivery.next_attempt_at <= now,
            )
            .order_by(WebhookDelivery.next_attempt_at)
            .limit(settings.webhook_dispatch_batch)
            .with_for_update(skip_locked=True, of=WebhookDelivery)
        ).all()
        picked = len(rows)
        for delivery, url, secret_encrypted in rows:
            attempt_delivery(delivery, url, secret_encrypted, now)
            if delivery.status == "delivered":
                delivered += 1
        db.commit()
    finally:
        if own_session:
            db.close()
    if picked:
        log.info(
            "webhook dispatch pass", extra=_f(picked=picked, delivered=delivered)
        )
    return delivered
