import uuid

from tests.conftest import ADMIN_HEADERS, bearer, create_host, report_payload


def _patch(client, host_id, body):
    return client.patch(f"/api/v1/admin/hosts/{host_id}", headers=ADMIN_HEADERS, json=body)


def test_deactivate_blocks_reports_then_reactivate(client):
    host_id, token = create_host(client)

    # active host can report
    assert client.post("/api/v1/reports", headers=bearer(token), json=report_payload()).status_code == 200

    r = _patch(client, host_id, {"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    # still listed, just flagged
    row = next(h for h in client.get("/api/v1/hosts").json() if h["id"] == host_id)
    assert row["is_active"] is False

    # the agent's token is now rejected
    assert client.post("/api/v1/reports", headers=bearer(token), json=report_payload()).status_code == 401

    assert _patch(client, host_id, {"is_active": True}).json()["is_active"] is True
    assert client.post("/api/v1/reports", headers=bearer(token), json=report_payload()).status_code == 200


def test_patch_empty_body_422(client):
    host_id, _ = create_host(client)
    assert _patch(client, host_id, {}).status_code == 422


def test_patch_both_fields_at_once(client):
    host_id, _ = create_host(client)
    r = _patch(client, host_id, {"reboot_policy": "auto", "is_active": False})
    assert r.status_code == 200
    body = r.json()
    assert body["reboot_policy"] == "auto" and body["is_active"] is False


def test_delete_host(client):
    host_id, _ = create_host(client)
    assert client.delete(f"/api/v1/admin/hosts/{host_id}", headers=ADMIN_HEADERS).status_code == 204
    assert client.get(f"/api/v1/hosts/{host_id}").status_code == 404
    assert host_id not in [h["id"] for h in client.get("/api/v1/hosts").json()]

    assert client.delete(
        f"/api/v1/admin/hosts/{uuid.uuid4()}", headers=ADMIN_HEADERS
    ).status_code == 404
    assert client.delete(f"/api/v1/admin/hosts/{host_id}").status_code == 422  # no admin key
