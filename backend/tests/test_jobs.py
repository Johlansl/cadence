import uuid
from datetime import datetime, timedelta, timezone

from app.models.models import Host, Job, WebhookDelivery
from tests.conftest import ADMIN_HEADERS, create_host, signed, webhook_row


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


def test_health_check_job_endpoint_creates_and_claims(client, db_session):
    host_id, token = create_host(client)

    r = client.post("/api/v1/agent/health-check-job", auth=signed(token))

    assert r.status_code == 200, r.text
    handoff = r.json()["job"]
    assert handoff is not None
    assert handoff["job_type"] == "health_check"
    assert "health_checks" in handoff["params"]

    job = db_session.get(Job, handoff["id"])
    assert job.status == "running"  # already claimed, no second call needed
    assert job.requested_by == "boot"
    assert db_session.get(Host, host_id).last_seen_at is not None


def test_health_check_job_endpoint_returns_none_when_host_busy(client, db_session):
    host_id, token = create_host(client)
    _make_job(client, host_id)  # an ordinary apt_upgrade job, still pending

    r = client.post("/api/v1/agent/health-check-job", auth=signed(token))

    assert r.status_code == 200, r.text
    assert r.json()["job"] is None
    assert db_session.query(Job).filter(Job.host_id == host_id).count() == 1


def test_health_check_job_endpoint_requires_signed_auth(client):
    r = client.post("/api/v1/agent/health-check-job")
    assert r.status_code == 401


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


def _phase(status: str, check_status: str, *, name: str = "disk_space") -> dict:
    return {
        "status": status,
        "checks": [
            {
                "name": name,
                "status": check_status,
                "summary": f"{name} is {check_status}",
                "details": {},
            }
        ],
    }


def test_upgrade_result_stores_action_and_unhealthy_post_checks_separately(
    client, db_session
):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={
            "status": "succeeded",
            "exit_code": 0,
            "pre_checks": _phase("passed", "passed"),
            "post_checks": _phase(
                "failed", "failed", name="failed_services"
            ),
            "health_status": "unhealthy",
        },
    )

    assert r.status_code == 200, r.text
    stored = db_session.get(Job, job_id)
    assert stored.status == "succeeded"
    assert stored.result["health_status"] == "unhealthy"
    assert stored.result["pre_checks"]["status"] == "passed"
    assert stored.result["post_checks"]["checks"][0]["name"] == "failed_services"
    host = db_session.get(Host, host_id)
    assert host.health_status == "unhealthy"
    assert host.health_checked_at == stored.completed_at


def test_upgrade_result_stores_a_pre_check_block_without_post_checks(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={
            "status": "failed",
            "exit_code": -1,
            "pre_checks": _phase("failed", "failed"),
            "post_checks": None,
            "health_status": "unknown",
        },
    )

    assert r.status_code == 200, r.text
    result = db_session.get(Job, job_id).result
    assert result["pre_checks"]["status"] == "failed"
    assert "post_checks" not in result
    assert result["health_status"] == "unknown"
    host = db_session.get(Host, host_id)
    assert host.health_status == "unknown"
    assert host.health_checked_at == db_session.get(Job, job_id).completed_at


def test_upgrade_result_rejects_inconsistent_health(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={
            "status": "succeeded",
            "exit_code": 0,
            "pre_checks": _phase("passed", "passed"),
            "post_checks": _phase("failed", "failed"),
            "health_status": "healthy",
        },
    )

    assert r.status_code == 422
    assert db_session.get(Job, job_id).status == "running"


def test_upgrade_result_rejects_duplicate_checks(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))
    pre = _phase("passed", "passed")
    pre["checks"].append(dict(pre["checks"][0]))

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={
            "status": "succeeded",
            "exit_code": 0,
            "pre_checks": pre,
            "post_checks": _phase("passed", "passed"),
            "health_status": "healthy",
        },
    )

    assert r.status_code == 422


def test_non_upgrade_result_rejects_health_checks(client):
    host_id, token = create_host(client)
    job_id = _make_dry_run_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={
            "status": "succeeded",
            "exit_code": 0,
            "pre_checks": _phase("failed", "failed"),
            "health_status": "unknown",
        },
    )

    assert r.status_code == 422


def _make_dry_run_job(client, host_id: str) -> str:
    r = client.post(
        f"/api/v1/admin/hosts/{host_id}/jobs",
        headers=ADMIN_HEADERS,
        json={"job_type": "apt_dry_run"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_health_check_job(client, host_id: str) -> str:
    r = client.post(
        f"/api/v1/admin/hosts/{host_id}/jobs",
        headers=ADMIN_HEADERS,
        json={"job_type": "health_check"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_health_check_result_accepts_post_checks_without_pre_checks(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_health_check_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))  # -> running

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={
            "status": "succeeded",
            "exit_code": 0,
            "post_checks": _phase("passed", "passed"),
            "health_status": "healthy",
        },
    )

    assert r.status_code == 200, r.text
    result = db_session.get(Job, job_id).result
    assert "pre_checks" not in result
    assert result["post_checks"]["status"] == "passed"
    assert result["health_status"] == "healthy"
    host = db_session.get(Host, host_id)
    assert host.health_status == "healthy"
    assert host.health_checked_at == db_session.get(Job, job_id).completed_at


def test_health_check_result_rejects_pre_checks(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_health_check_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={
            "status": "succeeded",
            "exit_code": 0,
            "pre_checks": _phase("passed", "passed"),
            "post_checks": _phase("passed", "passed"),
            "health_status": "healthy",
        },
    )

    assert r.status_code == 422
    assert db_session.get(Job, job_id).status == "running"


def test_health_check_result_requires_post_checks_and_health_status(client):
    for i, missing in enumerate(
        ({"health_status": "healthy"}, {"post_checks": _phase("passed", "passed")})
    ):
        # A rejected submission leaves the job "running" forever (nothing
        # rolls it back), so each case needs its own host.
        host_id, token = create_host(client, hostname=f"vm-hc-missing-{i}")
        job_id = _make_health_check_job(client, host_id)
        client.post("/api/v1/agent/next-job", auth=signed(token))
        r = client.post(
            f"/api/v1/jobs/{job_id}/result",
            auth=signed(token),
            json={"status": "succeeded", "exit_code": 0, **missing},
        )
        assert r.status_code == 422, missing


# The agent's bounded() (agent/internal/healthcheck/checks.go) truncates every
# check summary and evidence line to at most this many bytes, ellipsis
# included. It must land exactly on the server's own limit
# (app/schemas/schemas.py: HealthCheckIn.summary / EvidenceLine): one byte over
# is a 422 that would leave the job stuck running until the reaper, with the
# health evidence lost.
_EVIDENCE_LIMIT = 500


def _pre_check_block_result(*, summary_len: int, evidence_len: int) -> dict:
    return {
        "status": "failed",
        "exit_code": -1,
        "pre_checks": {
            "status": "failed",
            "checks": [
                {
                    "name": "disk_space",
                    "status": "failed",
                    "summary": "x" * summary_len,
                    "details": {"problems": ["y" * evidence_len]},
                }
            ],
        },
        "health_status": "unknown",
    }


def test_health_check_result_accepts_the_agent_truncation_ceiling(client, db_session):
    """Contract with the agent: a summary and an evidence line at exactly the
    500-byte ceiling bounded() truncates to are accepted and stored; one byte
    over is a 422, not a silently stranded job."""

    def submit(hostname, *, summary_len, evidence_len):
        host_id, token = create_host(client, hostname)
        job_id = _make_job(client, host_id)
        client.post("/api/v1/agent/next-job", auth=signed(token))  # -> running
        r = client.post(
            f"/api/v1/jobs/{job_id}/result",
            auth=signed(token),
            json=_pre_check_block_result(
                summary_len=summary_len, evidence_len=evidence_len
            ),
        )
        return job_id, r

    job_id, r = submit(
        "vm-ceil-ok", summary_len=_EVIDENCE_LIMIT, evidence_len=_EVIDENCE_LIMIT
    )
    assert r.status_code == 200, r.text
    stored = db_session.get(Job, job_id).result["pre_checks"]["checks"][0]
    assert len(stored["summary"]) == _EVIDENCE_LIMIT
    assert len(stored["details"]["problems"][0]) == _EVIDENCE_LIMIT

    _, r = submit(
        "vm-ceil-summary", summary_len=_EVIDENCE_LIMIT + 1, evidence_len=_EVIDENCE_LIMIT
    )
    assert r.status_code == 422

    _, r = submit(
        "vm-ceil-evidence", summary_len=_EVIDENCE_LIMIT, evidence_len=_EVIDENCE_LIMIT + 1
    )
    assert r.status_code == 422


def test_dry_run_result_is_stored_structured(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_dry_run_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))

    preview = {
        "updated": [
            {
                "name": "openssl",
                "architecture": "amd64",
                "installed_version": "3.0.11-1",
                "candidate_version": "3.0.14-1",
                "is_security_update": True,
            }
        ],
        "newly_installed": [],
        "removed": [{"name": "obsolete-lib", "installed_version": "4.5-6"}],
        "kept_back": ["docker-ce"],
        "excluded": ["linux-image-amd64"],
        "held_in_place": ["docker-ce"],
    }
    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={"status": "succeeded", "exit_code": 0, "dry_run": preview},
    )
    assert r.status_code == 200
    stored = db_session.get(Job, job_id).result["dry_run"]
    assert stored["updated"][0]["candidate_version"] == "3.0.14-1"
    assert stored["removed"][0]["name"] == "obsolete-lib"
    assert stored["kept_back"] == ["docker-ce"]
    assert stored["excluded"] == ["linux-image-amd64"]
    assert r.json()["result"]["dry_run"]["held_in_place"] == ["docker-ce"]


def test_dry_run_key_absent_when_not_sent(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={"status": "succeeded", "exit_code": 0},
    )
    assert r.status_code == 200
    assert "dry_run" not in db_session.get(Job, job_id).result


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


def _race_job_result(first: dict, second: dict, *, job_type: str = "apt_upgrade"):
    """Race two truly independent transactions submitting one running job.

    Each side runs on its own connection with its own commit, overlapping on
    a pre-read barrier so both observe ``running``. Rows are committed on
    their own connections (outside any rolled-back test transaction) because
    the race only exists across real concurrent transactions; they are
    deleted at teardown. Returns (winner_tag, stored_job, deliveries, host).
    """
    import threading

    from fastapi import HTTPException
    from sqlalchemy.orm import sessionmaker

    from app.api.routes.jobs import submit_job_result
    from app.db.base import engine
    from app.models.models import Host, Job, Webhook, WebhookDelivery
    from app.schemas.schemas import JobResultIn
    from tests.conftest import webhook_row

    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    setup = maker()
    try:
        host = Host(
            hostname=f"vm-race-{uuid.uuid4().hex[:8]}",
            os_family="debian",
            package_manager="apt",
            reboot_required=True,
        )
        setup.add(host)
        setup.flush()
        host_id = host.id
        hook_id = webhook_row(setup, events=("job.succeeded", "job.failed")).id
        job = Job(
            host_id=host_id,
            job_type=job_type,
            status="running",
            started_at=datetime.now(timezone.utc),
        )
        setup.add(job)
        setup.flush()
        job_id = job.id
        setup.commit()
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    outcomes: dict[str, tuple] = {}

    def _submit(tag: str, payload: dict) -> None:
        db = maker()
        try:
            own_host = db.get(Host, host_id)
            db.get(Job, job_id)  # pre-read: both sides overlap on "running"
            barrier.wait(timeout=30)
            submit_job_result(job_id, JobResultIn(**payload), own_host, db)
            outcomes[tag] = ("ok", None)
        except HTTPException as exc:
            db.rollback()
            outcomes[tag] = ("http", exc.status_code)
        finally:
            db.close()

    try:
        threads = [
            threading.Thread(target=_submit, args=("a", first), daemon=True),
            threading.Thread(target=_submit, args=("b", second), daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert all(not thread.is_alive() for thread in threads), outcomes
        assert sorted(kind for kind, _ in outcomes.values()) == ["http", "ok"], outcomes
        (winner_tag,) = [tag for tag, (kind, _) in outcomes.items() if kind == "ok"]
        (loser_code,) = [code for kind, code in outcomes.values() if kind == "http"]
        assert loser_code == 409, outcomes

        check = maker()
        try:
            stored = check.get(Job, job_id)
            host = check.get(Host, host_id)
            deliveries = [
                d
                for d in check.query(WebhookDelivery).all()
                if (d.payload.get("data") or {}).get("job_id") == str(job_id)
            ]
            return winner_tag, stored, deliveries, host
        finally:
            check.close()
    finally:
        cleanup = maker()
        try:
            cleanup.query(WebhookDelivery).filter(
                WebhookDelivery.webhook_id == hook_id
            ).delete()
            cleanup.query(Job).filter(Job.host_id == host_id).delete()
            cleanup.query(Webhook).filter(Webhook.id == hook_id).delete()
            cleanup.query(Host).filter(Host.id == host_id).delete()
            cleanup.commit()
        finally:
            cleanup.close()


def test_concurrent_result_one_winner_succeeded_vs_succeeded(db_session):
    winner, stored, deliveries, _ = _race_job_result(
        {"status": "succeeded", "exit_code": 0, "log": "race-a"},
        {"status": "succeeded", "exit_code": 0, "log": "race-b"},
    )
    expected_log = f"race-{winner}"
    assert stored.status == "succeeded"
    assert stored.log == expected_log  # no blend of the two submissions
    assert len(deliveries) == 1  # the loser stages no outbox row
    assert deliveries[0].event_type == "job.succeeded"
    assert deliveries[0].payload["data"]["status"] == "succeeded"


def test_concurrent_result_one_winner_succeeded_vs_failed(db_session):
    # Every final field must come from one winner: no blend across submissions.
    winner, stored, deliveries, host = _race_job_result(
        {"status": "succeeded", "exit_code": 0, "log": "race-a"},
        {
            "status": "failed",
            "exit_code": 1,
            "log": "race-b",
            "failure_category": "dpkg_error",
            "failure_summary": "E: dpkg broke",
        },
    )
    expected = {
        "a": {
            "status": "succeeded",
            "exit_code": 0,
            "failure_category": None,
            "failure_summary": None,
        },
        "b": {
            "status": "failed",
            "exit_code": 1,
            "failure_category": "dpkg_error",
            "failure_summary": "E: dpkg broke",
        },
    }[winner]
    assert stored.status == expected["status"]
    assert stored.log == f"race-{winner}"
    assert stored.result["exit_code"] == expected["exit_code"]
    assert stored.failure_category == expected["failure_category"]
    assert stored.failure_summary == expected["failure_summary"]
    assert len(deliveries) == 1
    assert deliveries[0].event_type == f"job.{expected['status']}"
    data = deliveries[0].payload["data"]
    assert data["status"] == expected["status"]
    assert data["job_id"] == str(stored.id)
    assert data.get("failure_category") == expected["failure_category"]
    assert data.get("failure_summary") == expected["failure_summary"]
    # Neither side sends health: the host projection stays untouched,
    # proving the loser wrote nothing anywhere.
    assert host.health_status == "unknown"
    assert host.health_checked_at is None


def test_concurrent_result_one_winner_failed_vs_failed(db_session):
    winner, stored, deliveries, _ = _race_job_result(
        {"status": "failed", "exit_code": 1, "log": "race-a"},
        {"status": "failed", "exit_code": 2, "log": "race-b"},
    )
    assert stored.status == "failed"
    assert stored.log == f"race-{winner}"
    assert stored.result["exit_code"] == {"a": 1, "b": 2}[winner]
    assert len(deliveries) == 1
    assert deliveries[0].event_type == "job.failed"


def test_concurrent_result_host_projection_from_winner_only(db_session):
    # A reboot job's success clears reboot_required: the host must reflect
    # the winner alone, never a blend with the loser.
    winner, stored, deliveries, host = _race_job_result(
        {"status": "succeeded", "exit_code": 0, "log": "race-a"},
        {"status": "failed", "exit_code": 1, "log": "race-b"},
        job_type="reboot",
    )
    assert stored.status == {"a": "succeeded", "b": "failed"}[winner]
    assert host.reboot_required == (winner == "b")
    assert len(deliveries) == 1


def test_second_submit_has_no_side_effects(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))  # -> running
    webhook_row(db_session, events=("job.succeeded", "job.failed"))

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={"status": "succeeded", "exit_code": 0, "log": "first"},
    )
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.query(WebhookDelivery).count() == 1

    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={"status": "failed", "exit_code": 1, "log": "late"},
    )
    assert r.status_code == 409
    db_session.expire_all()
    stored = db_session.get(Job, job_id)
    assert stored.status == "succeeded"
    assert stored.log == "first"  # the late submit changed nothing
    assert db_session.query(WebhookDelivery).count() == 1  # no second event

    # Unknown job stays 404.
    r = client.post(
        f"/api/v1/jobs/{uuid.uuid4()}/result",
        auth=signed(token),
        json={"status": "succeeded", "exit_code": 0},
    )
    assert r.status_code == 404
