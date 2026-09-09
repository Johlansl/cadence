"""excluded_count / per-package excluded, on GET /hosts and GET /hosts/{id}."""

from __future__ import annotations

from tests.conftest import ADMIN_HEADERS, create_host, pkg, report_payload, signed


def _report(client, token, packages, hostname="vm-test"):
    r = client.post(
        "/api/v1/reports",
        auth=signed(token),
        json=report_payload(hostname=hostname, packages=packages),
    )
    assert r.status_code == 200, r.text


def _exclusion(client, **over):
    body = {"scope": "global", "pattern": "linux-image*"}
    body.update(over)
    r = client.post("/api/v1/admin/package-exclusions", headers=ADMIN_HEADERS, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_get_host_excluded_count_and_per_package_flag(client):
    host_id, token = create_host(client)
    _report(
        client,
        token,
        [
            pkg("linux-image-6.1.0-amd64", candidate="6.6.0"),
            pkg("docker-ce", candidate="27.0"),
            pkg("curl", candidate="8.1"),
        ],
    )
    _exclusion(client, pattern="linux-image*")

    r = client.get(f"/api/v1/hosts/{host_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["updates_available_count"] == 3
    assert body["excluded_count"] == 1
    flags = {p["name"]: p["excluded"] for p in body["packages"]}
    assert flags == {
        "linux-image-6.1.0-amd64": True,
        "docker-ce": False,
        "curl": False,
    }


def test_excluded_flag_applies_before_a_candidate_exists(client):
    host_id, token = create_host(client)
    # No candidate_version: nothing pending, but the pattern still matches.
    _report(client, token, [pkg("linux-image-6.1.0-amd64")])
    _exclusion(client, pattern="linux-image*")

    r = client.get(f"/api/v1/hosts/{host_id}")
    body = r.json()
    assert body["updates_available_count"] == 0
    # Shown on the row (protected pre-emptively)...
    assert body["packages"][0]["excluded"] is True
    # ...but not counted, since there is nothing pending to exclude from.
    assert body["excluded_count"] == 0


def test_no_policy_means_zero(client):
    host_id, token = create_host(client)
    _report(client, token, [pkg("curl", candidate="8.1")])

    r = client.get(f"/api/v1/hosts/{host_id}")
    body = r.json()
    assert body["excluded_count"] == 0
    assert body["packages"][0]["excluded"] is False


def test_list_hosts_excluded_count(client):
    host_a, token_a = create_host(client, hostname="a")
    host_b, token_b = create_host(client, hostname="b")
    _report(client, token_a, [pkg("docker-ce", candidate="27.0")], hostname="a")
    _report(client, token_b, [pkg("curl", candidate="8.1")], hostname="b")
    _exclusion(client, pattern="docker-ce")

    r = client.get("/api/v1/hosts")
    assert r.status_code == 200
    counts = {row["hostname"]: row["excluded_count"] for row in r.json()}
    assert counts == {"a": 1, "b": 0}
