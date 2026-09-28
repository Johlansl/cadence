"""6A lot 2: boot observation plumbing, engine untouched.

The agent reports its boot identifier and its own reboot decision; the server
persists both and tracks each host's current boot. Nothing here changes what
the campaign engine does with them yet (lot 3).
"""

from app.models.models import Host, Job
from tests.conftest import ADMIN_HEADERS, create_host, report_payload, signed


def _report(client, token, **overrides):
    r = client.post("/api/v1/reports", auth=signed(token), json=report_payload(**overrides))
    assert r.status_code == 200, r.text


def _make_job(client, host_id):
    r = client.post(
        f"/api/v1/admin/hosts/{host_id}/jobs", headers=ADMIN_HEADERS, json={}
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _submit(client, token, job_id, **fields):
    payload = {"status": "succeeded", "exit_code": 0, "log": "ok"}
    payload.update(fields)
    r = client.post(f"/api/v1/jobs/{job_id}/result", auth=signed(token), json=payload)
    assert r.status_code == 200, r.text


def test_report_boot_id_is_recorded(client, db_session):
    host_id, token = create_host(client)
    assert db_session.get(Host, host_id).current_boot_id is None

    _report(client, token, boot_id="boot-1")
    assert db_session.get(Host, host_id).current_boot_id == "boot-1"

    _report(client, token, boot_id="boot-2")
    assert db_session.get(Host, host_id).current_boot_id == "boot-2"


def test_report_without_boot_id_leaves_observation_untouched(client, db_session):
    host_id, token = create_host(client)
    _report(client, token, boot_id="boot-1")

    _report(client, token)  # old agent: no boot_id key at all
    assert db_session.get(Host, host_id).current_boot_id == "boot-1"


def test_poll_boot_id_is_recorded_and_optional_body_accepted(client, db_session):
    host_id, token = create_host(client)

    r = client.post("/api/v1/agent/next-job", auth=signed(token), json={"boot_id": "boot-9"})
    assert r.status_code == 200
    assert db_session.get(Host, host_id).current_boot_id == "boot-9"

    # Old agents POST "{}" (or nothing): still 200, observation untouched.
    r = client.post("/api/v1/agent/next-job", auth=signed(token), json={})
    assert r.status_code == 200
    r = client.post("/api/v1/agent/next-job", auth=signed(token))
    assert r.status_code == 200
    assert db_session.get(Host, host_id).current_boot_id == "boot-9"


def test_boot_health_check_ask_records_boot_id(client, db_session):
    host_id, token = create_host(client)

    r = client.post(
        "/api/v1/agent/health-check-job", auth=signed(token), json={"boot_id": "boot-7"}
    )
    assert r.status_code == 200, r.text
    assert db_session.get(Host, host_id).current_boot_id == "boot-7"


def test_result_stores_boot_id_and_explicit_will_reboot(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))  # -> running

    _submit(client, token, job_id, boot_id="boot-3", will_reboot=True)
    result = db_session.get(Job, job_id).result
    assert result["boot_id"] == "boot-3"
    assert result["will_reboot"] is True
    assert db_session.get(Host, host_id).current_boot_id == "boot-3"


def test_result_explicit_will_reboot_false_is_preserved(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))  # -> running

    _submit(client, token, job_id, will_reboot=False)
    result = db_session.get(Job, job_id).result
    assert result["will_reboot"] is False
    assert "boot_id" not in result


def test_result_without_boot_fields_stays_byte_compatible(client, db_session):
    host_id, token = create_host(client)
    job_id = _make_job(client, host_id)
    client.post("/api/v1/agent/next-job", auth=signed(token))  # -> running

    _submit(client, token, job_id, reboot_required=False)
    assert db_session.get(Job, job_id).result == {
        "exit_code": 0,
        "reboot_required": False,
    }
    assert db_session.get(Host, host_id).current_boot_id is None


def test_creation_pins_host_policy_and_override_wins(client, db_session):
    host_id, token = create_host(client)
    client.patch(
        f"/api/v1/admin/hosts/{host_id}",
        headers=ADMIN_HEADERS,
        json={"reboot_policy": "auto"},
    )

    pinned = _make_job(client, host_id)
    assert db_session.get(Job, pinned).params["reboot"] == "auto"

    client.post("/api/v1/agent/next-job", auth=signed(token))  # -> running
    _submit(client, token, pinned)

    overridden = client.post(
        f"/api/v1/admin/hosts/{host_id}/jobs",
        headers=ADMIN_HEADERS,
        json={"params": {"reboot": "never"}},
    ).json()["id"]
    assert db_session.get(Job, overridden).params["reboot"] == "never"


def test_pinned_mode_survives_policy_flip_before_claim(client, db_session):
    host_id, token = create_host(client)
    client.patch(
        f"/api/v1/admin/hosts/{host_id}",
        headers=ADMIN_HEADERS,
        json={"reboot_policy": "auto"},
    )
    job_id = _make_job(client, host_id)
    client.patch(
        f"/api/v1/admin/hosts/{host_id}",
        headers=ADMIN_HEADERS,
        json={"reboot_policy": "never"},
    )

    handoff = client.post("/api/v1/agent/next-job", auth=signed(token)).json()["job"]
    assert handoff["id"] == job_id
    assert handoff["params"]["reboot"] == "auto"
    assert db_session.get(Job, job_id).params["reboot"] == "auto"


def test_unpinned_legacy_row_still_falls_back_to_live_policy(client, db_session):
    host_id, token = create_host(client)
    client.patch(
        f"/api/v1/admin/hosts/{host_id}",
        headers=ADMIN_HEADERS,
        json={"reboot_policy": "auto"},
    )
    job_id = _make_job(client, host_id)

    # Simulate a row created before pinning existed: no reboot key at all.
    job = db_session.get(Job, job_id)
    params = dict(job.params)
    del params["reboot"]
    job.params = params
    db_session.flush()

    handoff = client.post("/api/v1/agent/next-job", auth=signed(token)).json()["job"]
    assert handoff["params"]["reboot"] == "auto"
