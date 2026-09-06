from sqlalchemy import select

from app.core.config import settings
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


def test_report_refuses_empty_packages_when_host_has_inventory(client, db_session):
    host_id, token = create_host(client)

    client.post(
        "/api/v1/reports",
        headers=bearer(token),
        json=report_payload(packages=[pkg("bash"), pkg("openssl", candidate="3.1")]),
    )

    r = client.post(
        "/api/v1/reports", headers=bearer(token), json=report_payload(packages=[])
    )
    assert r.status_code == 422, r.text

    rows = db_session.execute(
        select(HostPackage).where(HostPackage.host_id == host_id)
    ).scalars().all()
    assert len(rows) == 2

    # The rejected report was not logged.
    reports = db_session.execute(
        select(Report).where(Report.host_id == host_id)
    ).scalars().all()
    assert len(reports) == 1


def test_report_accepts_empty_packages_for_fresh_host(client):
    _, token = create_host(client)
    r = client.post(
        "/api/v1/reports", headers=bearer(token), json=report_payload(packages=[])
    )
    assert r.status_code == 200, r.text
    assert r.json()["installed_package_count"] == 0


def test_report_keeps_descriptive_fields_when_omitted(client, db_session):
    host_id, token = create_host(client)

    # A full report populates the descriptive metadata.
    client.post(
        "/api/v1/reports",
        headers=bearer(token),
        json=report_payload(
            fqdn="vm-test.lan", os_name="Debian GNU/Linux", os_version="13"
        ),
    )

    # A later report that omits those fields must not blank the stored values.
    partial = report_payload()
    for field in ("fqdn", "os_name", "os_version"):
        partial.pop(field, None)
    r = client.post("/api/v1/reports", headers=bearer(token), json=partial)
    assert r.status_code == 200, r.text

    host = db_session.get(Host, host_id)
    assert host.fqdn == "vm-test.lan"
    assert host.os_name == "Debian GNU/Linux"
    assert host.os_version == "13"


def test_report_persists_source_package_and_codename(client, db_session):
    host_id, token = create_host(client)

    r = client.post(
        "/api/v1/reports",
        headers=bearer(token),
        json=report_payload(
            os_codename="bookworm",
            packages=[pkg("libssl3", candidate="3.0.14", security=True, source="openssl")],
        ),
    )
    assert r.status_code == 200, r.text

    host = db_session.get(Host, host_id)
    assert host.os_codename == "bookworm"
    row = db_session.execute(
        select(HostPackage).where(HostPackage.host_id == host_id)
    ).scalar_one()
    assert row.source_package == "openssl"


def test_report_accepts_pre_070_payload_without_source_fields(client, db_session):
    host_id, token = create_host(client)

    # No os_codename, no per-package source_package (a 0.6.x agent).
    r = client.post(
        "/api/v1/reports",
        headers=bearer(token),
        json=report_payload(packages=[pkg("bash")]),
    )
    assert r.status_code == 200, r.text

    host = db_session.get(Host, host_id)
    assert host.os_codename is None
    row = db_session.execute(
        select(HostPackage).where(HostPackage.host_id == host_id)
    ).scalar_one()
    assert row.source_package is None


def test_report_keeps_os_codename_when_omitted(client, db_session):
    host_id, token = create_host(client)

    client.post(
        "/api/v1/reports",
        headers=bearer(token),
        json=report_payload(os_codename="bookworm", packages=[pkg("bash")]),
    )
    # A later report that omits os_codename must not blank the stored value.
    partial = report_payload(packages=[pkg("bash")])
    partial.pop("os_codename", None)
    r = client.post("/api/v1/reports", headers=bearer(token), json=partial)
    assert r.status_code == 200, r.text

    assert db_session.get(Host, host_id).os_codename == "bookworm"


def test_report_rejects_oversized_body(client, monkeypatch):
    _, token = create_host(client)
    monkeypatch.setattr(settings, "max_report_bytes", 100)
    r = client.post(
        "/api/v1/reports",
        headers=bearer(token),
        json=report_payload(packages=[pkg("bash"), pkg("coreutils")]),
    )
    assert r.status_code == 413, r.text


def test_report_rejects_too_many_packages(client, monkeypatch):
    _, token = create_host(client)
    monkeypatch.setattr(settings, "max_report_packages", 3)
    r = client.post(
        "/api/v1/reports",
        headers=bearer(token),
        json=report_payload(packages=[pkg(f"p{i}") for i in range(4)]),
    )
    assert r.status_code == 422, r.text

    # At the cap is fine.
    r = client.post(
        "/api/v1/reports",
        headers=bearer(token),
        json=report_payload(packages=[pkg(f"p{i}") for i in range(3)]),
    )
    assert r.status_code == 200, r.text


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
