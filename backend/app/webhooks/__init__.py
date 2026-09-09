"""Outbound webhook notifications.

An event that happens in a request handler (a job result, a report transition)
or on the scheduler loop (a host going offline) is written to the
`webhook_deliveries` outbox in the same transaction as the change that produced
it, so a later HTTP failure never loses it. `dispatch_pending_deliveries` on
the scheduler tick drains the outbox with retry + exponential backoff.

`enqueue_event` / `enqueue_test` build and stage delivery rows;
`dispatch_pending_deliveries` signs and POSTs them; `scan_offline_hosts` is the
periodic host.offline detector. See docs/architecture.md "Webhooks".
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

# The event names a webhook can subscribe to. Kept in step with
# app.schemas.schemas.WebhookEventType. 'webhook.test' is not here: it is only
# ever sent to one explicitly targeted endpoint.
WEBHOOK_EVENT_TYPES: tuple[str, ...] = (
    "job.succeeded",
    "job.failed",
    "host.offline",
    "host.reboot_required",
    "host.security_updates_available",
)
TEST_EVENT_TYPE = "webhook.test"

_ELLIPSIS = "•••"


def _f(**fields: object) -> dict:
    """Wrap structured fields for the logfmt formatter (matches app.scheduler)."""
    return {"fields": fields}


def iso_z(dt: datetime) -> str:
    """UTC ISO 8601 with a trailing Z, the format used in every webhook body."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def mask_url(url: str) -> str:
    """A display form of a webhook URL: scheme + host + up to the first three
    path segments, with a bullet marker if anything further (a path tail, a
    query, a fragment) was dropped. Best-effort visual masking, not a security
    boundary: the raw URL is simply never returned outside WebhookCreated.

    https://discord.com/api/webhooks/123456/tok
        -> https://discord.com/api/webhooks/123456/•••
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url
    base = f"{parts.scheme}://{parts.netloc}"
    segments = [s for s in parts.path.split("/") if s]
    kept = segments[:3]
    masked = base + ("/" + "/".join(kept) if kept else "")
    if len(segments) > len(kept) or parts.query or parts.fragment:
        masked += "/" + _ELLIPSIS
    return masked


def truncate_log(text: str, max_bytes: int) -> str:
    """Cap a job log for a webhook body: keep the head and the tail, elide the
    middle. `max_bytes <= 0` keeps the whole log. Mirrors the agent's
    executor.capLog."""
    if max_bytes <= 0:
        return text
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    half = max_bytes // 2
    omitted = len(raw) - 2 * half
    head = raw[:half].decode("utf-8", errors="ignore")
    tail = raw[-half:].decode("utf-8", errors="ignore")
    return (
        f"{head}\n\n[cadence] ... {omitted} bytes of log elided ...\n\n{tail}"
    )


def event_body(
    event_type: str, data: dict, *, occurred_at: datetime, delivery_id: object
) -> dict:
    """The JSON body POSTed for one delivery. `timestamp` is when the event
    occurred (immutable across retries); the per-attempt send time goes in the
    X-Cadence-Timestamp header instead."""
    return {
        "event_type": event_type,
        "timestamp": iso_z(occurred_at),
        "delivery_id": str(delivery_id),
        "data": data,
    }
