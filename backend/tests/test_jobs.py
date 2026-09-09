import uuid
from datetime import datetime, timedelta, timezone

from app.models.models import Host, Job
from tests.conftest import ADMIN_HEADERS, create_host, signed


def _make_job(client, host_id: str) -> str:
    r = client.post(f"/api/v1/admin/hosts/{host_id}/jobs", headers=ADMIN_HEADERS, json={})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_next_job_claims_oldest_then_empty(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)

    r = client.post("/api/v1/agent/next-job", auth=signed(token))
    assert r.status_code == 200
    assert r.json()["job"]["id"] == job_id
    assert db_session.get(Job, job_id).status == "running"

    # last_seen_at is bumped by the poll, too.
    assert db_session.get(Host, host_id).last_seen_at is not None

    r = client.post("/api/v1/agent/next-job", auth=signed(token))
    assert r.status_code == 200
    assert r.json()["job"] is None


def test_job_result_transitions_and_conflicts(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))  # -> running

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={"status": "succeeded", "exit_code": 0, "log": "ok", "reboot_required": False},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "succeeded"
    assert db_session.get(Job, job_id).result == {"exit_code": 0, "reboot_required": False}

    # Second callback -> job no longer running.
    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={"status": "failed", "exit_code": 1},
    )
    assert r.status_code == 409


def test_job_result_stores_failure_classification(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))  # -> running

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={
            "status": "failed",
            "exit_code": 100,
            "log": "E: Sub-process /usr/bin/dpkg returned an error code (1)",
            "failure_category": "dpkg_error",
            "failure_summary": "E: Sub-process /usr/bin/dpkg returned an error code (1)",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["failure_category"] == "dpkg_error"
    assert body["failure_summary"].startswith("E: Sub-process")

    job = db_session.get(Job, job_id)
    assert job.failure_category == "dpkg_error"
    assert job.failure_summary.startswith("E: Sub-process")


def test_job_result_ignores_classification_on_success(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={
            "status": "succeeded",
            "exit_code": 0,
            "failure_category": "dpkg_error",
            "failure_summary": "should be dropped",
        },
    )
    assert r.status_code == 200
    assert r.json()["failure_category"] is None
    job = db_session.get(Job, job_id)
    assert job.failure_category is None and job.failure_summary is None


def test_job_result_from_older_agent_leaves_classification_null(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))

    # No failure_category / failure_summary keys at all (agent < 0.9.0).
    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={"status": "failed", "exit_code": 1, "log": "boom"},
    )
    assert r.status_code == 200
    assert r.json()["failure_category"] is None
    job = db_session.get(Job, job_id)
    assert job.failure_category is None and job.failure_summary is None


def test_job_result_clips_an_overlong_summary(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={
            "status": "failed",
            "exit_code": 1,
            "failure_category": "unknown",
            "failure_summary": "x" * 2000,
        },
    )
    assert r.status_code == 200
    assert len(db_session.get(Job, job_id).failure_summary) == 500


def test_job_result_stores_held_conflicts_on_success(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={
            "status": "succeeded",
            "exit_code": 0,
            "held_conflicts": ["docker-ce"],
        },
    )
    assert r.status_code == 200
    assert r.json()["result"]["held_conflicts"] == ["docker-ce"]
    assert db_session.get(Job, job_id).result["held_conflicts"] == ["docker-ce"]


def test_job_result_omits_held_conflicts_key_when_not_sent(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={"status": "succeeded", "exit_code": 0},
    )
    assert r.status_code == 200
    assert "held_conflicts" not in db_session.get(Job, job_id).result


def test_job_result_stores_held_packages(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={
            "status": "succeeded",
            "exit_code": 0,
            "held_packages": ["docker-ce", "postgresql-14"],
        },
    )
    assert r.status_code == 200
    assert r.json()["result"]["held_packages"] == ["docker-ce", "postgresql-14"]
    assert db_session.get(Job, job_id).result["held_packages"] == ["docker-ce", "postgresql-14"]


def test_job_result_omits_held_packages_key_when_not_sent(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={"status": "succeeded", "exit_code": 0},
    )
    assert r.status_code == 200
    assert "held_packages" not in db_session.get(Job, job_id).result


def test_job_result_hidden_from_other_host(client):
    host_a, _ = create_host(client, hostname="a")
    _host_b, token_b = create_host(client, hostname="b")
    job_id = _make_job(client, host_a)

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token_b),
        json={"status": "succeeded", "exit_code": 0},
    )
    assert r.status_code == 404


def test_clear_host_jobs(client):
    host_id, token = create_host(client)
    j1 = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))  # -> running
    client.post(
        f"/api/v1/jobs/{j1}/result",
        auth=signed(token),
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


def test_list_host_jobs_pagination(client, db_session):
    host_id, _ = create_host(client)
    base = datetime.now(timezone.utc)
    for i in range(5):
        db_session.add(
            Job(
                host_id=host_id,
                job_type="apt_upgrade",
                status="succeeded",
                created_at=base - timedelta(minutes=i),
            )
        )
    db_session.flush()

    page = client.get(f"/api/v1/hosts/{host_id}/jobs?limit=2").json()
    assert len(page) == 2

    more = client.get(
        f"/api/v1/hosts/{host_id}/jobs?limit=10&before={page[-1]['created_at']}"
    ).json()
    assert len(more) == 3
    assert all(j["created_at"] < page[-1]["created_at"] for j in more)
