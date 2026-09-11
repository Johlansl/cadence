from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.models import AuditLog, EnrollmentCode
from app.pki.client_ca import ensure_client_ca
from tests.conftest import ADMIN_HEADERS, create_host


def _server_ca(tmp_path, monkeypatch):
    material = ensure_client_ca(tmp_path / "server-ca")
    monkeypatch.setattr("app.api.routes.enrollments.settings.server_ca_file", str(material.root_certificate))
    return material.root_certificate


def test_create_new_host_enrollment_returns_secret_once(
    client, db_session, tmp_path, monkeypatch
):
    ca_file = _server_ca(tmp_path, monkeypatch)
    before = datetime.now(timezone.utc)
    response = client.post(
        "/api/v1/admin/enrollments",
        headers=ADMIN_HEADERS,
        json={
            "expected_hostname": " vm-new ",
            "label": "rack install",
            "tags": {"Role": "Web"},
            "reboot_policy": "prompt",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert re.fullmatch(r"cad1\.[A-Za-z0-9_-]{32}\.[0-9a-f]{64}", body["code"])
    assert body["expected_hostname"] == "vm-new"

    row = db_session.get(EnrollmentCode, body["id"])
    secret = body["code"].split(".")[1]
    assert row.secret_hash == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in row.secret_hash
    assert row.ca_fingerprint_sha256 == hashlib.sha256(ca_file.read_bytes()).hexdigest()
    assert row.tags == {"role": "web"}
    assert before + timedelta(minutes=29) < row.expires_at < before + timedelta(minutes=31)

    listed = client.get("/api/v1/admin/enrollments", headers=ADMIN_HEADERS).json()
    assert listed[0]["id"] == body["id"]
    assert listed[0]["state"] == "pending"
    assert "code" not in listed[0]
    assert "secret_hash" not in listed[0]

    audit = db_session.execute(
        select(AuditLog).where(AuditLog.action == "enrollment.create")
    ).scalar_one()
    assert audit.target_id == body["id"]


def test_existing_host_enrollment_and_revoke(client, db_session, tmp_path, monkeypatch):
    _server_ca(tmp_path, monkeypatch)
    host_id, _ = create_host(client, hostname="vm-existing")
    response = client.post(
        "/api/v1/admin/enrollments",
        headers=ADMIN_HEADERS,
        json={"target_host_id": host_id, "ttl_minutes": 5},
    )
    assert response.status_code == 201, response.text
    enrollment_id = response.json()["id"]

    assert client.delete(
        f"/api/v1/admin/enrollments/{enrollment_id}", headers=ADMIN_HEADERS
    ).status_code == 204
    assert client.delete(
        f"/api/v1/admin/enrollments/{enrollment_id}", headers=ADMIN_HEADERS
    ).status_code == 204
    listed = client.get(
        "/api/v1/admin/enrollments?state=revoked", headers=ADMIN_HEADERS
    ).json()
    assert [item["id"] for item in listed] == [enrollment_id]


def test_enrollment_target_and_ttl_validation(client, tmp_path, monkeypatch):
    _server_ca(tmp_path, monkeypatch)
    missing = "00000000-0000-0000-0000-000000000001"
    cases = [
        {},
        {"expected_hostname": "vm", "target_host_id": missing},
        {"expected_hostname": "vm", "ttl_minutes": 4},
        {"expected_hostname": "vm", "ttl_minutes": 241},
    ]
    for payload in cases:
        response = client.post(
            "/api/v1/admin/enrollments", headers=ADMIN_HEADERS, json=payload
        )
        assert response.status_code == 422, (payload, response.text)

    response = client.post(
        "/api/v1/admin/enrollments",
        headers=ADMIN_HEADERS,
        json={"target_host_id": missing},
    )
    assert response.status_code == 404


def test_create_enrollment_fails_closed_without_server_ca(client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.api.routes.enrollments.settings.server_ca_file", str(tmp_path / "missing.crt")
    )
    response = client.post(
        "/api/v1/admin/enrollments",
        headers=ADMIN_HEADERS,
        json={"expected_hostname": "vm-new"},
    )
    assert response.status_code == 503
