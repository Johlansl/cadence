import uuid

from app.models.models import Job
from tests.conftest import ADMIN_HEADERS, bearer, create_host


def _set_policy(client, host_id, value):
    return client.patch(
        f"/api/v1/admin/hosts/{host_id}", headers=ADMIN_HEADERS, json={"reboot_policy": value}
    )


def test_default_policy_is_never(client):
    host_id, _ = create_host(client)
    row = next(h for h in client.get("/api/v1/hosts").json() if h["id"] == host_id)
    assert row["reboot_policy"] == "never"
    assert client.get(f"/api/v1/hosts/{host_id}").json()["reboot_policy"] == "never"


def test_patch_reboot_policy(client):
    host_id, _ = create_host(client)

    r = _set_policy(client, host_id, "auto")
    assert r.status_code == 200
    assert r.json() == {
        "id": host_id,
        "hostname": "vm-test",
        "reboot_policy": "auto",
        "is_active": True,
        "tags": {},
    }
    assert client.get(f"/api/v1/hosts/{host_id}").json()["reboot_policy"] == "auto"

    assert _set_policy(client, host_id, "never").status_code == 200


def test_patch_rejects_bad_values(client):
    host_id, _ = create_host(client)
    assert _set_policy(client, host_id, "sometimes").status_code == 422
    r = client.patch(
        f"/api/v1/admin/hosts/{host_id}",
        headers={"X-Admin-Key": "wrong"},
        json={"reboot_policy": "auto"},
    )
    assert r.status_code == 401
    assert _set_policy(client, uuid.uuid4(), "auto").status_code == 404


def test_create_job_rejects_bad_reboot_override(client):
    host_id, _ = create_host(client)
    r = client.post(
        f"/api/v1/admin/hosts/{host_id}/jobs",
        headers=ADMIN_HEADERS,
        json={"params": {"reboot": "maybe"}},
    )
    assert r.status_code == 422


def _claim(client, token):
    return client.post("/api/v1/agent/next-job", headers=bearer(token)).json()["job"]


def test_effective_reboot_from_host_policy(client, db_session):
    host_id, token = create_host(client)
    _set_policy(client, host_id, "auto")
    job_id = client.post(
        f"/api/v1/admin/hosts/{host_id}/jobs", headers=ADMIN_HEADERS, json={}
    ).json()["id"]

    handoff = _claim(client, token)
    assert handoff["id"] == job_id
    assert handoff["params"]["reboot"] == "auto"
    assert db_session.get(Job, job_id).params["reboot"] == "auto"


def test_effective_reboot_defaults_never(client):
    host_id, token = create_host(client)  # policy defaults to never
    client.post(f"/api/v1/admin/hosts/{host_id}/jobs", headers=ADMIN_HEADERS, json={})
    assert _claim(client, token)["params"]["reboot"] == "never"


def test_per_job_override_wins_over_host_policy(client):
    host_id, token = create_host(client)
    _set_policy(client, host_id, "never")
    client.post(
        f"/api/v1/admin/hosts/{host_id}/jobs",
        headers=ADMIN_HEADERS,
        json={"params": {"reboot": "auto"}},
    )
    assert _claim(client, token)["params"]["reboot"] == "auto"


def test_per_job_override_can_disable_on_auto_host(client):
    host_id, token = create_host(client)
    _set_policy(client, host_id, "auto")
    client.post(
        f"/api/v1/admin/hosts/{host_id}/jobs",
        headers=ADMIN_HEADERS,
        json={"params": {"reboot": "never"}},
    )
    assert _claim(client, token)["params"]["reboot"] == "never"


# --- 'prompt' mode + dedicated reboot job (F-30) --------------------------


def test_reboot_policy_accepts_prompt(client):
    host_id, _ = create_host(client)
    assert _set_policy(client, host_id, "prompt").status_code == 200
    assert client.get(f"/api/v1/hosts/{host_id}").json()["reboot_policy"] == "prompt"


def test_prompt_host_upgrade_job_pins_prompt(client):
    host_id, token = create_host(client)
    _set_policy(client, host_id, "prompt")
    client.post(f"/api/v1/admin/hosts/{host_id}/jobs", headers=ADMIN_HEADERS, json={})
    assert _claim(client, token)["params"]["reboot"] == "prompt"


def test_dedicated_reboot_job(client, db_session):
    host_id, token = create_host(client)
    r = client.post(
        f"/api/v1/admin/hosts/{host_id}/jobs",
        headers=ADMIN_HEADERS,
        json={"job_type": "reboot"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["job_type"] == "reboot"

    handoff = _claim(client, token)
    assert handoff["job_type"] == "reboot"
    # no reboot pinned into params for a non-upgrade job
    assert "reboot" not in handoff["params"]


def test_unknown_job_type_rejected(client):
    host_id, _ = create_host(client)
    r = client.post(
        f"/api/v1/admin/hosts/{host_id}/jobs",
        headers=ADMIN_HEADERS,
        json={"job_type": "format_c"},
    )
    assert r.status_code == 422


def test_schedule_accepts_prompt_reboot_param(client):
    host_id, _ = create_host(client)
    r = client.post(
        f"/api/v1/admin/hosts/{host_id}/schedules",
        headers=ADMIN_HEADERS,
        json={
            "kind": "weekly",
            "weekday": 0,
            "hour": 3,
            "minute": 0,
            "timezone": "UTC",
            "params": {"reboot": "prompt"},
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["params"]["reboot"] == "prompt"
