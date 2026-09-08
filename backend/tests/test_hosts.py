import uuid

from tests.conftest import create_host, pkg, report_payload, signed


def _report(client, token, packages):
    r = client.post(
        "/api/v1/reports", auth=signed(token), json=report_payload(packages=packages)
    )
    assert r.status_code == 200, r.text


def test_host_status_precedence(client):
    host_id, token = create_host(client)

    def status():
        row = next(h for h in client.get("/api/v1/hosts").json() if h["id"] == host_id)
        return row["status"], row["updates_available_count"], row["security_updates_count"]

    _report(client, token, [pkg("a", candidate="2", security=True), pkg("b", candidate="2")])
    assert status() == ("security_updates_available", 2, 1)

    _report(client, token, [pkg("b", candidate="2"), pkg("c")])
    assert status() == ("updates_available", 1, 0)

    _report(client, token, [pkg("c"), pkg("d")])
    assert status() == ("up_to_date", 0, 0)


def test_host_detail_lists_packages(client):
    host_id, token = create_host(client)
    _report(client, token, [pkg("zlib"), pkg("openssl", candidate="3.1", security=True)])

    r = client.get(f"/api/v1/hosts/{host_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "security_updates_available"
    names = sorted(p["name"] for p in body["packages"])
    assert names == ["openssl", "zlib"]

    assert client.get(f"/api/v1/hosts/{uuid.uuid4()}").status_code == 404


def test_hosts_list_empty(client):
    assert client.get("/api/v1/hosts").json() == []
