"""Unit tests for the webhook outbox: enqueue, mask/truncate helpers, and the
dispatcher (signing, retry, backoff, attempt cap). The HTTP POST is stubbed."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.models.models import Webhook, WebhookDelivery
from app.webhooks import mask_url, truncate_log
from app.webhooks.delivery import (
    attempt_delivery,
    backoff_delay,
    dispatch_pending_deliveries,
)
from app.webhooks.enqueue import enqueue_event
from tests.conftest import webhook_row

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)


# --- pure helpers ----------------------------------------------------------


def test_mask_url_redacts_the_path_tail():
    assert (
        mask_url("https://discord.com/api/webhooks/123456/abcdefTOKEN")
        == "https://discord.com/api/webhooks/123456/•••"
    )


def test_mask_url_short_url_untouched_but_query_flagged():
    assert mask_url("https://example.test/hook") == "https://example.test/hook"
    assert mask_url("https://example.test/hook?t=x") == "https://example.test/hook/•••"


def test_truncate_log_keeps_head_and_tail():
    assert truncate_log("short", 4096) == "short"
    assert truncate_log("x" * 100, 0) == "x" * 100
    out = truncate_log("A" * 50 + "B" * 50, 20)
    assert out.startswith("A" * 10)
    assert out.endswith("B" * 10)
    assert "80 bytes of log elided" in out


# --- enqueue -------------------------------------------------------------


def test_enqueue_only_for_subscribed_enabled_webhooks(db_session):
    webhook_row(db_session, events=("job.succeeded",), url="https://a.test/h")
    webhook_row(db_session, events=("job.failed",), url="https://b.test/h")
    webhook_row(
        db_session, events=("job.succeeded",), enabled=False, url="https://c.test/h"
    )

    staged = enqueue_event(db_session, "job.succeeded", {"job_id": "1"}, occurred_at=NOW)
    db_session.flush()

    assert staged == 1
    rows = db_session.query(WebhookDelivery).all()
    assert [r.event_type for r in rows] == ["job.succeeded"]
    body = rows[0].payload
    assert body["event_type"] == "job.succeeded"
    assert body["timestamp"] == "2026-09-09T12:00:00Z"
    assert body["delivery_id"] == str(rows[0].id)
    assert body["data"] == {"job_id": "1"}


def test_enqueue_noop_when_globally_disabled(db_session, monkeypatch):
    webhook_row(db_session, events=("job.succeeded",))
    monkeypatch.setattr(settings, "webhooks_enabled", False)
    assert enqueue_event(db_session, "job.succeeded", {}, occurred_at=NOW) == 0


# --- dispatcher --------------------------------------------------------


def _pending(db, hook: Webhook, *, event_type="job.succeeded") -> WebhookDelivery:
    enqueue_event(db, event_type, {"k": "v"}, occurred_at=NOW)
    db.flush()
    return db.query(WebhookDelivery).filter_by(webhook_id=hook.id).one()


def test_dispatch_success_marks_delivered_and_signs(db_session, monkeypatch):
    hook = webhook_row(db_session, secret="whsec-abc", events=("job.succeeded",))
    delivery = _pending(db_session, hook)

    seen: dict = {}

    def fake_post(url, body_bytes, headers, timeout):
        seen.update(url=url, body=body_bytes, headers=headers, timeout=timeout)
        return 200

    monkeypatch.setattr("app.webhooks.delivery._post", fake_post)

    delivered = dispatch_pending_deliveries(now=NOW, db=db_session)

    assert delivered == 1
    db_session.refresh(delivery)
    assert delivery.status == "delivered"
    assert delivery.completed_at == NOW
    assert delivery.attempt_count == 1
    assert delivery.last_error is None

    assert seen["url"] == "https://example.test/hook"
    assert seen["timeout"] == settings.webhook_timeout_seconds
    assert seen["headers"]["X-Cadence-Event"] == "job.succeeded"
    assert seen["headers"]["X-Cadence-Delivery"] == str(delivery.id)
    # Signature recomputes with the raw secret over "{ts}\n{sha256(body)}".
    ts = seen["headers"]["X-Cadence-Timestamp"]
    body_hash = hashlib.sha256(seen["body"]).hexdigest()
    expected = hmac.new(
        b"whsec-abc", f"{ts}\n{body_hash}".encode(), hashlib.sha256
    ).hexdigest()
    assert seen["headers"]["X-Cadence-Signature"] == expected
    # The signed body is the stored payload, canonicalised.
    assert json.loads(seen["body"]) == delivery.payload


def test_dispatch_retries_with_backoff_then_gives_up(db_session, monkeypatch):
    monkeypatch.setattr(settings, "webhook_max_attempts", 3)
    hook = webhook_row(db_session, events=("job.succeeded",))
    delivery = _pending(db_session, hook)

    def boom(url, body_bytes, headers, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr("app.webhooks.delivery._post", boom)

    # attempt 1 -> still pending, next try in 60s
    dispatch_pending_deliveries(now=NOW, db=db_session)
    db_session.refresh(delivery)
    assert delivery.status == "pending"
    assert delivery.attempt_count == 1
    assert delivery.next_attempt_at == NOW + timedelta(seconds=60)
    assert "connection refused" in delivery.last_error

    # not yet due
    assert dispatch_pending_deliveries(now=NOW + timedelta(seconds=30), db=db_session) == 0

    # attempt 2 -> pending, next try in 120s
    dispatch_pending_deliveries(now=NOW + timedelta(seconds=61), db=db_session)
    db_session.refresh(delivery)
    assert delivery.attempt_count == 2
    assert delivery.next_attempt_at == NOW + timedelta(seconds=61) + timedelta(seconds=120)

    # attempt 3 -> hits the cap, parked failed
    dispatch_pending_deliveries(now=NOW + timedelta(hours=1), db=db_session)
    db_session.refresh(delivery)
    assert delivery.status == "failed"
    assert delivery.attempt_count == 3
    assert delivery.completed_at is not None


def test_dispatch_treats_non_2xx_as_failure(db_session, monkeypatch):
    monkeypatch.setattr(settings, "webhook_max_attempts", 1)
    hook = webhook_row(db_session, events=("job.succeeded",))
    delivery = _pending(db_session, hook)
    monkeypatch.setattr("app.webhooks.delivery._post", lambda *a, **k: 500)

    dispatch_pending_deliveries(now=NOW, db=db_session)

    db_session.refresh(delivery)
    assert delivery.status == "failed"
    assert delivery.last_error == "HTTP 500"


def test_dispatch_respects_batch_size(db_session, monkeypatch):
    monkeypatch.setattr(settings, "webhook_dispatch_batch", 2)
    for i in range(3):
        webhook_row(db_session, events=("job.succeeded",), url=f"https://h{i}.test/x")
    enqueue_event(db_session, "job.succeeded", {}, occurred_at=NOW)
    db_session.flush()
    monkeypatch.setattr("app.webhooks.delivery._post", lambda *a, **k: 200)

    assert dispatch_pending_deliveries(now=NOW, db=db_session) == 2
    assert dispatch_pending_deliveries(now=NOW, db=db_session) == 1


def test_backoff_delay_caps_at_one_hour():
    assert backoff_delay(1) == timedelta(seconds=60)
    assert backoff_delay(2) == timedelta(seconds=120)
    assert backoff_delay(50) == timedelta(seconds=3600)


def test_attempt_fails_cleanly_when_secret_undecryptable(db_session, monkeypatch):
    monkeypatch.setattr(settings, "webhook_max_attempts", 1)
    hook = webhook_row(db_session, events=("job.succeeded",))
    hook.secret_encrypted = "not-a-fernet-token"
    db_session.flush()
    delivery = _pending(db_session, hook)

    called = False

    def fake_post(*a, **k):
        nonlocal called
        called = True
        return 200

    monkeypatch.setattr("app.webhooks.delivery._post", fake_post)
    attempt_delivery(delivery, hook.url, hook.secret_encrypted, NOW)

    assert called is False
    assert delivery.status == "failed"
    assert "cannot be decrypted" in delivery.last_error
