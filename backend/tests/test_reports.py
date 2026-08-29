from sqlalchemy import select

from app.models.models import Host, HostPackage, Job, Report
from tests.conftest import (
    ADMIN_HEADERS,
    bearer,
    create_host,
    pkg,
    report_payload,
)


def test_report_requires_bearer_token(client):
    r = client.post("/api/v1/reports", json=report_payload())
    assert r.status_code == 422

    r = client.post(
        "/api/v1/reports", headers={"Authorization": "Bearer nope"}, json=report_payload()
    )
    assert r.status_code == 401


def test_report_replaces_package_state_and_counts(client, db_session):
    host_id, token = create_host(client)

    first = report_payload(
        packages=[
            pkg("openssl", candidate="3.1", security=True),
            pkg("vim", candidate="9.1"),
            pkg("bash"),
        ]
    )
    r = client.post("/api/v1/reports", headers=bearer(token), json=first)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["installed_package_count"] == 3
    assert body["updates_available_count"] == 2
    assert body["security_updates_count"] == 1
    assert body["job"] is None

    # A second report fully replaces host_packages.
    second = report_payload(packages=[pkg("bash"), pkg("coreutils")])
    r = client.post("/api/v1/reports", headers=bearer(token), json=second)
    assert r.status_code == 200
    assert r.json()["updates_available_count"] == 0

    rows = db_session.execute(
        select(HostPackage).where(HostPackage.host_id == host_id)
    ).scalars().all()
    assert len(rows) == 2

    # Both reports were logged with their raw payload.
    reports = db_session.execute(
        select(Report).where(Report.host_id == host_id).order_by(Report.received_at)
    ).scalars().all()
    assert len(reports) == 2
    assert reports[0].raw_payload["packages"][0]["name"] == "openssl"


def test_report_refreshes_host_metadata_and_last_seen(client, db_session):
    host_id, token = create_host(client, hostname="registered-name")

    client.post(
        "/api/v1/reports",
        headers=bearer(token),
        json=report_payload(hostname="real-name", os_version="13", reboot_required=True),
    )

    host = db_session.get(Host, host_id)
    assert host.hostname == "real-name"  # agent's hostname wins
    assert host.os_version == "13"
    assert host.reboot_required is True
    assert host.last_seen_at is not None
    assert host.agent_version == "test"


def test_report_piggybacks_pending_job(client, db_session):
    host_id, token = create_host(client)
    jr = client.post(f"/api/v1/admin/hosts/{host_id}/jobs", headers=ADMIN_HEADERS, json={})
    job_id = jr.json()["id"]

    r = client.post("/api/v1/reports", headers=bearer(token), json=report_payload())
    assert r.status_code == 200
    handoff = r.json()["job"]
    assert handoff is not None and handoff["id"] == job_id
    assert handoff["job_type"] == "apt_upgrade"

    job = db_session.get(Job, job_id)
    assert job.status == "running"
    assert job.started_at is not None
