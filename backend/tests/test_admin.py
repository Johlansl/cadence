import uuid

from tests.conftest import ADMIN_HEADERS, create_host


def test_create_host_returns_token_once(client):
    r = client.post(
        "/api/v1/admin/hosts",
        headers=ADMIN_HEADERS,
        json={"hostname": "vm-web-01", "description": "web"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    uuid.UUID(body["id"])  # valid UUID
    assert body["hostname"] == "vm-web-01"
    assert isinstance(body["token"], str) and len(body["token"]) >= 32


def test_create_host_rejects_bad_admin_key(client):
    r = client.post(
        "/api/v1/admin/hosts",
        headers={"X-Admin-Key": "wrong"},
        json={"hostname": "nope"},
    )
    assert r.status_code == 401


def test_create_host_requires_admin_key_header(client):
    r = client.post("/api/v1/admin/hosts", json={"hostname": "nope"})
    assert r.status_code == 422  # missing required header


def test_create_job_conflicts_when_one_is_active(client):
    host_id, _ = create_host(client)

    r1 = client.post(f"/api/v1/admin/hosts/{host_id}/jobs", headers=ADMIN_HEADERS, json={})
    assert r1.status_code == 201, r1.text

    r2 = client.post(f"/api/v1/admin/hosts/{host_id}/jobs", headers=ADMIN_HEADERS, json={})
    assert r2.status_code == 409


def test_create_job_unknown_host_404(client):
    r = client.post(
        f"/api/v1/admin/hosts/{uuid.uuid4()}/jobs", headers=ADMIN_HEADERS, json={}
    )
    assert r.status_code == 404
