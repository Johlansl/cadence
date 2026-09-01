from tests.conftest import ADMIN_HEADERS, create_host


def test_tags_default_empty_and_patch_roundtrip(client):
    host_id, _ = create_host(client)

    assert client.get(f"/api/v1/hosts/{host_id}").json()["tags"] == {}

    r = client.patch(
        f"/api/v1/admin/hosts/{host_id}",
        headers=ADMIN_HEADERS,
        json={"tags": {"env": "prod", "role": "web"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["tags"] == {"env": "prod", "role": "web"}

    assert client.get(f"/api/v1/hosts/{host_id}").json()["tags"] == {
        "env": "prod",
        "role": "web",
    }
    listed = client.get("/api/v1/hosts").json()
    assert next(h for h in listed if h["id"] == host_id)["tags"] == {
        "env": "prod",
        "role": "web",
    }


def test_tags_are_replaced_wholesale(client):
    host_id, _ = create_host(client)
    client.patch(
        f"/api/v1/admin/hosts/{host_id}",
        headers=ADMIN_HEADERS,
        json={"tags": {"a": "1", "b": "2"}},
    )
    r = client.patch(
        f"/api/v1/admin/hosts/{host_id}", headers=ADMIN_HEADERS, json={"tags": {"a": "9"}}
    )
    assert r.json()["tags"] == {"a": "9"}


def test_tags_validation(client):
    host_id, _ = create_host(client)
    for bad in ({"k" * 41: "x"}, {"": "x"}, {"k": "v" * 81}):
        r = client.patch(
            f"/api/v1/admin/hosts/{host_id}", headers=ADMIN_HEADERS, json={"tags": bad}
        )
        assert r.status_code == 422, (bad, r.text)
