"""The report and job-result handlers stage webhook deliveries."""

from __future__ import annotations

from app.core.config import settings
from app.models.models import WebhookDelivery, WebhookHostState
from tests.conftest import (
    ADMIN_HEADERS,
    create_host,
    pkg,
    report_payload,
    signed,
    webhook_row,
)


def _deliveries(db, event_type: str | None = None) -> list[WebhookDelivery]:
    rows = db.query(WebhookDelivery).all()
    if event_type is not None:
        rows = [r for r in rows if r.event_type == event_type]
    return rows


def _run_job(client, host_id: str, token: str, *, status: str, log: str = "ok") -> str:
    r = client.post(
        f"/api/v1/admin/hosts/{host_id}/jobs", headers=ADMIN_HEADERS, json={}
    )
    assert r.status_code == 201, r.text
    job_id = r.json()["id"]
    client.post("/api/v1/agent/next-job", auth=signed(token))  # pending -> running
    r = client.post(
        f"/api/v1/jobs/{job_id}/result",
        auth=signed(token),
        json={"status": status, "exit_code": 0 if status == "succeeded" else 1, "log": log},
    )
    assert r.status_code == 200, r.text
    return job_id


# --- job.* -------------------------------------------------------------


def test_job_result_enqueues_only_the_matching_event(client, db_session):
    host_id, token = create_host(client)
    webhook_row(db_session, events=("job.succeeded",), url="https://ok.test/h")
    webhook_row(db_session, events=("job.failed",), url="https://ko.test/h")
    db_session.flush()

    job_id = _run_job(client, host_id, token, status="succeeded", log="all good")

    rows = _deliveries(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "job.succeeded"
    assert row.payload["data"]["job_id"] == job_id
    assert row.payload["data"]["host_id"] == host_id
    assert row.payload["data"]["exit_code"] == 0
    assert row.payload["data"]["log"] == "all good"
    assert row.payload["data"]["job_type"] == "apt_upgrade"


def test_job_failed_event(client, db_session):
    host_id, token = create_host(client)
    webhook_row(db_session, events=("job.failed",))
    db_session.flush()

    _run_job(client, host_id, token, status="failed")

    rows = _deliveries(db_session, "job.failed")
    assert len(rows) == 1
    assert rows[0].payload["data"]["exit_code"] == 1


def test_job_log_is_truncated_in_the_payload(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "webhook_log_max_bytes", 40)
    host_id, token = create_host(client)
    webhook_row(db_session, events=("job.succeeded",))
    db_session.flush()

    _run_job(client, host_id, token, status="succeeded", log="X" * 500)

    log = _deliveries(db_session, "job.succeeded")[0].payload["data"]["log"]
    assert "bytes of log elided" in log
    assert len(log.encode()) < 500


def test_nothing_enqueued_when_webhooks_disabled(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "webhooks_enabled", False)
    host_id, token = create_host(client)
    webhook_row(db_session, events=("job.succeeded",))
    db_session.flush()

    _run_job(client, host_id, token, status="succeeded")

    assert _deliveries(db_session) == []


# --- host.reboot_required -------------------------------------------


def test_reboot_required_fires_once_on_the_rising_edge(client, db_session):
    host_id, token = create_host(client)
    webhook_row(db_session, events=("host.reboot_required",))
    db_session.flush()

    # false -> false: nothing
    client.post(
        "/api/v1/reports", auth=signed(token), json=report_payload(reboot_required=False)
    )
    assert _deliveries(db_session, "host.reboot_required") == []

    # false -> true: one event
    client.post(
        "/api/v1/reports", auth=signed(token), json=report_payload(reboot_required=True)
    )
    assert len(_deliveries(db_session, "host.reboot_required")) == 1

    # true -> true: still one
    client.post(
        "/api/v1/reports", auth=signed(token), json=report_payload(reboot_required=True)
    )
    assert len(_deliveries(db_session, "host.reboot_required")) == 1


# --- host.security_updates_available -------------------------------


def _report_with_security(client, token, n: int):
    packages = [
        pkg(f"lib{i}", candidate="2.0", security=True) for i in range(n)
    ] or [pkg("bash", candidate=None)]
    return client.post(
        "/api/v1/reports", auth=signed(token), json=report_payload(packages=packages)
    )


def test_security_updates_event_on_change_only(client, db_session):
    host_id, token = create_host(client)
    webhook_row(db_session, events=("host.security_updates_available",))
    db_session.flush()

    _report_with_security(client, token, 3)  # 0 -> 3: fire
    _report_with_security(client, token, 3)  # 3 -> 3: no
    _report_with_security(client, token, 5)  # 3 -> 5: fire
    _report_with_security(client, token, 0)  # 5 -> 0: no (count is zero)

    rows = _deliveries(db_session, "host.security_updates_available")
    assert [r.payload["data"]["security_updates_count"] for r in rows] == [3, 5]
    assert rows[0].payload["data"]["previous_count"] is None
    assert rows[1].payload["data"]["previous_count"] == 3

    state = db_session.get(WebhookHostState, host_id)
    assert state.security_updates_notified == 5


# --- offline flag reset -------------------------------------------


def test_report_clears_the_offline_flag(client, db_session):
    host_id, token = create_host(client)
    webhook_row(db_session, events=("host.offline",))
    db_session.add(WebhookHostState(host_id=host_id, offline_notified=True))
    db_session.flush()

    client.post("/api/v1/reports", auth=signed(token), json=report_payload())

    assert db_session.get(WebhookHostState, host_id).offline_notified is False
