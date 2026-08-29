import uuid

from app.models.models import Host, Job
from tests.conftest import ADMIN_HEADERS, bearer, create_host


def _make_job(client, host_id: str) -> str:
    r = client.post(f"/api/v1/admin/hosts/{host_id}/jobs", headers=ADMIN_HEADERS, json={})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_next_job_claims_oldest_then_empty(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)

    r = client.post("/api/v1/agent/next-job", headers=bearer(token))
    assert r.status_code == 200
    assert r.json()["job"]["id"] == job_id
    assert db_session.get(Job, job_id).status == "running"

    # last_seen_at is bumped by the poll, too.
    assert db_session.get(Host, host_id).last_seen_at is not None

    r = client.post("/api/v1/agent/next-job", headers=bearer(token))
    assert r.status_code == 200
    assert r.json()["job"] is None


def test_job_result_transitions_and_conflicts(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", headers=bearer(token))  # -> running

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        headers=bearer(token),
        json={"status": "succeeded", "exit_code": 0, "log": "ok", "reboot_required": False},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "succeeded"
    assert db_session.get(Job, job_id).result == {"exit_code": 0, "reboot_required": False}

    # Second callback -> job no longer running.
    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        headers=bearer(token),
        json={"status": "failed", "exit_code": 1},
    )
    assert r.status_code == 409


def test_job_result_hidden_from_other_host(client):
    host_a, _ = create_host(client, hostname="a")
    _host_b, token_b = create_host(client, hostname="b")
    job_id = _make_job(client, host_a)

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        headers=bearer(token_b),
        json={"status": "succeeded", "exit_code": 0},
    )
    assert r.status_code == 404


def test_clear_host_jobs(client):
    host_id, token = create_host(client)
    j1 = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", headers=bearer(token))  # -> running
    client.post(
        f"/api/v1/jobs/{j1}/result",
        headers=bearer(token),
        json={"status": "succeeded", "exit_code": 0},
    )
    _make_job(client, host_id)  # a second, pending
    assert len(client.get(f"/api/v1/hosts/{host_id}/jobs").json()) == 2

    r = client.delete(f"/api/v1/admin/hosts/{host_id}/jobs", headers=ADMIN_HEADERS)
    assert r.status_code == 204
    assert client.get(f"/api/v1/hosts/{host_id}/jobs").json() == []

    assert client.delete(
        f"/api/v1/admin/hosts/{uuid.uuid4()}/jobs", headers=ADMIN_HEADERS
    ).status_code == 404
    assert client.delete(f"/api/v1/admin/hosts/{host_id}/jobs").status_code == 422  # no key


def test_job_views(client):
    host_id, _ = create_host(client)
    job_id = _make_job(client, host_id)

    r = client.get(f"/api/v1/hosts/{host_id}/jobs")
    assert r.status_code == 200
    assert [j["id"] for j in r.json()] == [job_id]

    r = client.get(f"/api/v1/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "pending"

    assert client.get(f"/api/v1/jobs/{uuid.uuid4()}").status_code == 404
