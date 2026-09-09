"""Pattern -> exact package name resolution, and its injection into a new
apt_upgrade job's params.excluded_packages."""

from __future__ import annotations

from app.exclusions import matching
from tests.conftest import ADMIN_HEADERS, create_host, pkg, report_payload, signed


def _report(client, token, packages):
    r = client.post(
        "/api/v1/reports", auth=signed(token), json=report_payload(packages=packages)
    )
    assert r.status_code == 200, r.text


def _exclusion(client, **over):
    body = {"scope": "global", "pattern": "linux-image*"}
    body.update(over)
    r = client.post("/api/v1/admin/package-exclusions", headers=ADMIN_HEADERS, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _create_job(client, host_id, **over):
    body = {}
    body.update(over)
    return client.post(f"/api/v1/admin/hosts/{host_id}/jobs", headers=ADMIN_HEADERS, json=body)


# --- matching() -----------------------------------------------------------


def test_matching_glob():
    names = ["linux-image-6.1.0-amd64", "nvidia-driver-535", "docker-ce", "curl"]
    assert matching(names, ["linux-image*"]) == ["linux-image-6.1.0-amd64"]
    assert matching(names, ["nvidia*"]) == ["nvidia-driver-535"]
    assert matching(names, ["docker-ce"]) == ["docker-ce"]  # exact, no wildcard
    assert matching(names, ["zzz*"]) == []


def test_matching_case_sensitive_and_deduped():
    assert matching(["Docker-CE", "docker-ce"], ["docker-ce"]) == ["docker-ce"]
    assert matching(["a", "a", "b"], ["a"]) == ["a"]


def test_matching_no_patterns_short_circuits():
    assert matching(["anything"], []) == []


# --- resolution against a real host's inventory ----------------------------


def test_resolves_against_full_inventory_not_just_pending(client):
    host_id, token = create_host(client)
    # linux-image has no pending update (candidate=None) -- must still be
    # held pre-emptively, per the "full inventory" design.
    _report(client, token, [pkg("linux-image-6.1.0-amd64"), pkg("curl", candidate="8.1")])
    _exclusion(client, pattern="linux-image*")

    job = _create_job(client, host_id).json()
    assert job["params"]["excluded_packages"] == ["linux-image-6.1.0-amd64"]


def test_global_and_host_scopes_are_additive(client):
    host_id, token = create_host(client)
    _report(
        client, token,
        [pkg("docker-ce"), pkg("postgresql-14"), pkg("nvidia-driver-535"), pkg("curl")],
    )
    _exclusion(client, scope="global", pattern="docker-ce")
    _exclusion(client, scope="host", host_id=host_id, pattern="postgresql-14")
    # a rule on a different host must not leak in
    other_host, _ = create_host(client, hostname="other")
    _exclusion(client, scope="host", host_id=other_host, pattern="nvidia*")

    job = _create_job(client, host_id).json()
    assert job["params"]["excluded_packages"] == ["docker-ce", "postgresql-14"]


def test_only_applies_to_apt_upgrade(client):
    host_id, token = create_host(client)
    _report(client, token, [pkg("docker-ce")])
    _exclusion(client, pattern="docker-ce")

    job = _create_job(client, host_id, job_type="reboot").json()
    assert "excluded_packages" not in job["params"]


def test_no_matching_policy_resolves_empty(client):
    host_id, token = create_host(client)
    _report(client, token, [pkg("curl")])

    job = _create_job(client, host_id).json()
    assert job["params"]["excluded_packages"] == []


def test_client_supplied_excluded_packages_is_always_overwritten(client):
    host_id, token = create_host(client)
    _report(client, token, [pkg("docker-ce")])
    _exclusion(client, pattern="docker-ce")

    job = _create_job(
        client, host_id, params={"excluded_packages": ["something-the-caller-made-up"]}
    ).json()
    assert job["params"]["excluded_packages"] == ["docker-ce"]


def test_scheduler_created_job_also_gets_excluded_packages(client, db_session):
    from datetime import datetime, timedelta, timezone

    from app.models.models import Schedule
    from app.scheduler import tick

    host_id, token = create_host(client)
    _report(client, token, [pkg("docker-ce")])
    _exclusion(client, pattern="docker-ce")

    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.add(
        Schedule(
            host_id=host_id,
            enabled=True,
            kind="weekly",
            weekday=0,
            hour=3,
            minute=0,
            timezone="UTC",
            params={},
            next_run_at=past,
        )
    )
    db_session.commit()

    assert tick(db=db_session) == 1

    r = client.get(f"/api/v1/hosts/{host_id}/jobs")
    jobs = r.json()
    assert len(jobs) == 1
    assert jobs[0]["params"]["excluded_packages"] == ["docker-ce"]
