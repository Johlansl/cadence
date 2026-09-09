"""Webhook admin CRUD + test-trigger routes."""

from __future__ import annotations

import uuid

from app.models.models import AuditLog, WebhookDelivery
from tests.conftest import ADMIN_HEADERS


def _create(client, **over):
    body = {
        "url": "https://hooks.example.test/api/webhooks/42/aaaaaaaaaaaaaaaa",
        "event_types": ["job.succeeded", "job.failed"],
    }
    body.update(over)
    return client.post("/api/v1/admin/webhooks", headers=ADMIN_HEADERS, json=body)


def test_create_returns_url_and_secret_once(client):
    r = _create(client, description="ops channel")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["url"] == "https://hooks.example.test/api/webhooks/42/aaaaaaaaaaaaaaaa"
    assert len(body["secret"]) >= 32
    assert body["event_types"] == ["job.succeeded", "job.failed"]
    assert body["enabled"] is True
    assert "secret_encrypted" not in body and "url_preview" not in body


def test_create_validation(client):
    assert _create(client, url="ftp://nope").status_code == 422
    assert _create(client, url="not a url").status_code == 422
    assert _create(client, event_types=[]).status_code == 422
    assert _create(client, event_types=["job.bogus"]).status_code == 422
    # auth
    assert client.post(
        "/api/v1/admin/webhooks", json={"url": "https://x.test", "event_types": ["job.failed"]}
    ).status_code == 422  # missing header
    assert _create(client, **{}).status_code == 201
    r = client.post(
        "/api/v1/admin/webhooks",
        headers={"X-Admin-Key": "wrong"},
        json={"url": "https://x.test/h", "event_types": ["job.failed"]},
    )
    assert r.status_code == 401


def test_list_masks_url_and_hides_secret(client):
    _create(client)
    r = client.get("/api/v1/webhooks")
    assert r.status_code == 200
    row = r.json()[0]
    assert row["url_preview"] == "https://hooks.example.test/api/webhooks/42/•••"
    assert "url" not in row and "secret" not in row
    assert row["event_types"] == ["job.succeeded", "job.failed"]
    assert row["pending_count"] == 0 and row["failed_count"] == 0
    assert row["last_success_at"] is None


def test_get_single_and_404(client):
    wid = _create(client).json()["id"]
    assert client.get(f"/api/v1/webhooks/{wid}").status_code == 200
    assert client.get(f"/api/v1/webhooks/{uuid.uuid4()}").status_code == 404


def test_patch_toggles_and_revalidates(client):
    wid = _create(client).json()["id"]

    r = client.patch(
        f"/api/v1/admin/webhooks/{wid}", headers=ADMIN_HEADERS, json={"enabled": False}
    )
    assert r.status_code == 200 and r.json()["enabled"] is False

    r = client.patch(
        f"/api/v1/admin/webhooks/{wid}",
        headers=ADMIN_HEADERS,
        json={"event_types": ["host.offline"]},
    )
    assert r.json()["event_types"] == ["host.offline"]

    # a bad url in the patch is rejected by the re-validation
    assert client.patch(
        f"/api/v1/admin/webhooks/{wid}", headers=ADMIN_HEADERS, json={"url": "nope"}
    ).status_code == 422
    # empty patch
    assert client.patch(
        f"/api/v1/admin/webhooks/{wid}", headers=ADMIN_HEADERS, json={}
    ).status_code == 422
    assert client.patch(
        f"/api/v1/admin/webhooks/{uuid.uuid4()}", headers=ADMIN_HEADERS, json={"enabled": False}
    ).status_code == 404


def test_delete_cascades_deliveries(client, db_session):
    wid = _create(client).json()["id"]
    client.post(f"/api/v1/admin/webhooks/{wid}/test", headers=ADMIN_HEADERS)
    assert db_session.query(WebhookDelivery).count() == 1

    r = client.delete(f"/api/v1/admin/webhooks/{wid}", headers=ADMIN_HEADERS)
    assert r.status_code == 204
    assert db_session.query(WebhookDelivery).count() == 0
    assert client.get(f"/api/v1/webhooks/{wid}").status_code == 404
    assert client.delete(
        f"/api/v1/admin/webhooks/{uuid.uuid4()}", headers=ADMIN_HEADERS
    ).status_code == 404


def test_test_trigger_enqueues_one_pending_delivery(client, db_session):
    wid = _create(client, enabled=False).json()["id"]  # even while disabled

    r = client.post(f"/api/v1/admin/webhooks/{wid}/test", headers=ADMIN_HEADERS)
    assert r.status_code == 202
    delivery_id = r.json()["delivery_id"]

    row = db_session.query(WebhookDelivery).one()
    assert str(row.id) == delivery_id
    assert row.webhook_id == uuid.UUID(wid)
    assert row.event_type == "webhook.test"
    assert row.status == "pending"
    assert row.payload["data"]["webhook_id"] == wid

    assert client.post(
        f"/api/v1/admin/webhooks/{uuid.uuid4()}/test", headers=ADMIN_HEADERS
    ).status_code == 404


def test_audit_trail(client, db_session):
    wid = _create(client).json()["id"]
    client.patch(
        f"/api/v1/admin/webhooks/{wid}", headers=ADMIN_HEADERS, json={"enabled": False}
    )
    client.post(f"/api/v1/admin/webhooks/{wid}/test", headers=ADMIN_HEADERS)
    client.delete(f"/api/v1/admin/webhooks/{wid}", headers=ADMIN_HEADERS)

    actions = [
        a
        for (a,) in db_session.query(AuditLog.action)
        .filter(AuditLog.target_type == "webhook")
        .order_by(AuditLog.id)
        .all()
    ]
    assert actions == [
        "webhook.create",
        "webhook.update",
        "webhook.test",
        "webhook.delete",
    ]
    create_detail = (
        db_session.query(AuditLog.detail)
        .filter(AuditLog.action == "webhook.create")
        .scalar()
    )
    assert create_detail["url_preview"] == "https://hooks.example.test/api/webhooks/42/•••"
