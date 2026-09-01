import uuid

from tests.conftest import ADMIN_HEADERS, create_host

WEEKLY = {"kind": "weekly", "weekday": 6, "hour": 3, "minute": 0, "timezone": "UTC"}


def _post(client, host_id, body):
    return client.post(
        f"/api/v1/admin/hosts/{host_id}/schedules", headers=ADMIN_HEADERS, json=body
    )


def test_crud_roundtrip(client):
    host_id, _ = create_host(client)

    assert client.get(f"/api/v1/hosts/{host_id}/schedules").json() == []

    r = _post(client, host_id, {**WEEKLY, "params": {"reboot": "auto"}})
    assert r.status_code == 201, r.text
    sched = r.json()
    assert sched["kind"] == "weekly" and sched["weekday"] == 6
    assert sched["day_of_month"] is None
    assert sched["next_run_at"] is not None
    assert sched["params"] == {"reboot": "auto"}
    sid = sched["id"]

    # one per host
    assert _post(client, host_id, WEEKLY).status_code == 409

    listed = client.get(f"/api/v1/hosts/{host_id}/schedules").json()
    assert [s["id"] for s in listed] == [sid]

    # switch to monthly via PATCH; day_of_month required, weekday cleared
    r = client.patch(
        f"/api/v1/admin/schedules/{sid}",
        headers=ADMIN_HEADERS,
        json={"kind": "monthly", "day_of_month": 12, "weekday": None},
    )
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "monthly"
    assert r.json()["weekday"] is None and r.json()["day_of_month"] == 12

    # disabling clears next_run_at
    r = client.patch(
        f"/api/v1/admin/schedules/{sid}", headers=ADMIN_HEADERS, json={"enabled": False}
    )
    assert r.json()["enabled"] is False
    assert r.json()["next_run_at"] is None

    assert client.delete(
        f"/api/v1/admin/schedules/{sid}", headers=ADMIN_HEADERS
    ).status_code == 204
    assert client.get(f"/api/v1/hosts/{host_id}/schedules").json() == []


def test_validation(client):
    host_id, _ = create_host(client)

    bad_bodies = [
        {"kind": "monthly", "weekday": 3, "hour": 2},  # monthly + weekday
        {"kind": "monthly", "day_of_month": 31, "hour": 2},  # >28
        {"kind": "weekly", "hour": 2},  # weekly, no weekday
        {"kind": "weekly", "weekday": 9, "hour": 2},  # weekday out of range
        {**WEEKLY, "hour": 24},
        {**WEEKLY, "timezone": "Mars/Olympus"},
        {**WEEKLY, "params": {"reboot": "perhaps"}},
    ]
    for body in bad_bodies:
        assert _post(client, host_id, body).status_code == 422, body


def test_auth_and_not_found(client):
    host_id, _ = create_host(client)
    assert client.post(
        f"/api/v1/admin/hosts/{host_id}/schedules", headers={"X-Admin-Key": "no"}, json=WEEKLY
    ).status_code == 401
    assert _post(client, uuid.uuid4(), WEEKLY).status_code == 404
    assert client.patch(
        f"/api/v1/admin/schedules/{uuid.uuid4()}", headers=ADMIN_HEADERS, json={"hour": 4}
    ).status_code == 404
    assert client.delete(
        f"/api/v1/admin/schedules/{uuid.uuid4()}", headers=ADMIN_HEADERS
    ).status_code == 404
