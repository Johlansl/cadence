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


# --- keyset pagination -------------------------------------------------------


def _walk(client, limit=2, **params):
    """Page through every group at `limit`, following (after, after_id) =
    the last row's (name, architecture)."""
    seen: list[tuple[str, str]] = []
    qs = {"limit": limit, "status": "all", **params}
    for _ in range(100):  # safety stop
        page = client.get("/api/v1/packages", params=qs).json()
        seen.extend((p["name"], p["architecture"]) for p in page)
        if len(page) < limit:
            break
        last = page[-1]
        qs = {
            "limit": limit,
            "status": "all",
            "after": last["name"],
            "after_id": last["architecture"],
            **params,
        }
    return seen


def test_packages_default_limit_is_50(client):
    host_id, token = create_host(client)
    _report(client, token, [pkg(f"p{i:03d}") for i in range(60)])

    body = client.get("/api/v1/packages", params={"status": "all"}).json()
    assert [p["name"] for p in body] == [f"p{i:03d}" for i in range(50)]


def test_packages_custom_limit_returns_first_n_by_key(client):
    host_id, token = create_host(client)
    _report(client, token, [pkg(f"p{i:03d}") for i in range(10)])

    body = client.get("/api/v1/packages", params={"status": "all", "limit": 5}).json()
    assert [p["name"] for p in body] == [f"p{i:03d}" for i in range(5)]


def test_packages_keyset_walks_every_group_once_with_arch_tiebreaker(client):
    host_id, token = create_host(client)
    _report(
        client,
        token,
        [
            pkg("acl"),
            pkg("libc6", architecture="amd64"),
            pkg("libc6", architecture="arm64"),
            pkg("zlib"),
        ],
    )

    paged = _walk(client, limit=2)
    full = [
        (p["name"], p["architecture"])
        for p in client.get("/api/v1/packages", params={"status": "all", "limit": 500}).json()
    ]

    assert full == [("acl", "amd64"), ("libc6", "amd64"), ("libc6", "arm64"), ("zlib", "amd64")]
    assert paged == full  # same set, same order, no repeats, no gaps
    assert len(set(paged)) == len(paged)


def test_packages_pagination_consistent_with_name_filter(client):
    host_id, token = create_host(client)
    _report(client, token, [pkg("lib-a"), pkg("lib-b"), pkg("other")])

    paged = _walk(client, limit=1, name="lib")
    assert paged == [("lib-a", "amd64"), ("lib-b", "amd64")]
    assert ("other", "amd64") not in paged


def test_packages_after_without_after_id_is_strict_on_name(client):
    host_id, token = create_host(client)
    _report(
        client,
        token,
        [pkg("libc6", architecture="amd64"), pkg("libc6", architecture="arm64"), pkg("zzz")],
    )

    body = client.get(
        "/api/v1/packages", params={"status": "all", "after": "libc6"}
    ).json()
    # every group whose name == "libc6" is excluded, both arches
    assert [p["name"] for p in body] == ["zzz"]


def test_packages_limit_bounds_rejected(client):
    assert client.get("/api/v1/packages", params={"limit": 0}).status_code == 422
    assert client.get("/api/v1/packages", params={"limit": 501}).status_code == 422
