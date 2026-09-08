from tests.conftest import create_host, pkg, report_payload, signed


def _report(client, token, packages, hostname="vm-test"):
    r = client.post(
        "/api/v1/reports",
        auth=signed(token),
        json=report_payload(packages=packages, hostname=hostname),
    )
    assert r.status_code == 200, r.text


def test_packages_default_status_is_pending(client):
    host_id, token = create_host(client)
    _report(client, token, [pkg("openssl", candidate="3.1", security=True), pkg("zlib")])

    r = client.get("/api/v1/packages")
    assert r.status_code == 200
    names = [p["name"] for p in r.json()]
    assert names == ["openssl"]


def test_packages_status_all_includes_up_to_date(client):
    host_id, token = create_host(client)
    _report(client, token, [pkg("openssl", candidate="3.1", security=True), pkg("zlib")])

    r = client.get("/api/v1/packages", params={"status": "all"})
    names = sorted(p["name"] for p in r.json())
    assert names == ["openssl", "zlib"]


def test_packages_status_security_excludes_plain_updates(client):
    host_id, token = create_host(client)
    _report(client, token, [pkg("openssl", candidate="3.1", security=True), pkg("bash", candidate="5.2")])

    r = client.get("/api/v1/packages", params={"status": "security"})
    names = [p["name"] for p in r.json()]
    assert names == ["openssl"]


def test_packages_name_filter_is_substring_case_insensitive(client):
    host_id, token = create_host(client)
    _report(client, token, [pkg("openssl", candidate="3.1"), pkg("zlib", candidate="1.3")])

    r = client.get("/api/v1/packages", params={"status": "all", "name": "SSL"})
    names = [p["name"] for p in r.json()]
    assert names == ["openssl"]


def test_packages_groups_multiple_hosts_under_one_package(client):
    host_a, token_a = create_host(client, "vm-a")
    host_b, token_b = create_host(client, "vm-b")
    _report(client, token_a, [pkg("openssl", candidate="3.1", security=True)], hostname="vm-a")
    _report(client, token_b, [pkg("openssl", candidate="3.1", security=True)], hostname="vm-b")

    r = client.get("/api/v1/packages")
    body = r.json()
    assert len(body) == 1
    assert body[0]["name"] == "openssl"
    hostnames = sorted(h["hostname"] for h in body[0]["hosts"])
    assert hostnames == ["vm-a", "vm-b"]


def test_packages_no_match_returns_empty_list(client):
    host_id, token = create_host(client)
    _report(client, token, [pkg("zlib")])

    r = client.get("/api/v1/packages", params={"name": "nonexistent"})
    assert r.json() == []
