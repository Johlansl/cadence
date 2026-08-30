from tests.conftest import bearer, create_host, pkg, report_payload


def test_report_history_newest_first_without_payload(client):
    hid, token = create_host(client)
    for _ in range(3):
        client.post(
            "/api/v1/reports",
            headers=bearer(token),
            json=report_payload(
                packages=[pkg("bash"), pkg("openssl", candidate="3.1", security=True)]
            ),
        )

    rows = client.get(f"/api/v1/hosts/{hid}/reports").json()
    assert len(rows) == 3
    assert "raw_payload" not in rows[0]
    assert rows[0]["updates_available_count"] == 1
    assert rows[0]["security_updates_count"] == 1
    assert rows[0]["received_at"] >= rows[-1]["received_at"]


def test_report_history_limit_and_before(client):
    hid, token = create_host(client)
    for _ in range(5):
        client.post("/api/v1/reports", headers=bearer(token), json=report_payload())

    page = client.get(f"/api/v1/hosts/{hid}/reports?limit=2").json()
    assert len(page) == 2

    older = client.get(
        f"/api/v1/hosts/{hid}/reports?before={page[-1]['received_at']}"
    ).json()
    assert len(older) == 3
    assert all(r["received_at"] < page[-1]["received_at"] for r in older)


def test_report_history_unknown_host_404(client):
    r = client.get("/api/v1/hosts/00000000-0000-0000-0000-000000000000/reports")
    assert r.status_code == 404
