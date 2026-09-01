from datetime import datetime, timedelta, timezone

from app.models.models import Job
from tests.conftest import bearer, create_host, pkg, report_payload


def test_fleet_summary_aggregates(client, db_session):
    h1, t1 = create_host(client, hostname="a")
    h2, t2 = create_host(client, hostname="b")
    create_host(client, hostname="c")  # never reports -> counts as silent

    client.post(
        "/api/v1/reports",
        headers=bearer(t1),
        json=report_payload(packages=[pkg("openssl", candidate="3.1", security=True), pkg("bash")]),
    )
    client.post(
        "/api/v1/reports",
        headers=bearer(t2),
        json=report_payload(packages=[pkg("vim", candidate="9.1"), pkg("bash")], reboot_required=True),
    )

    now = datetime.now(timezone.utc)
    db_session.add(Job(host_id=h1, job_type="apt_upgrade", status="succeeded",
                       completed_at=now - timedelta(hours=1)))
    db_session.add(Job(host_id=h2, job_type="apt_upgrade", status="failed",
                       completed_at=now - timedelta(hours=2)))
    db_session.add(Job(host_id=h1, job_type="apt_upgrade", status="failed",
                       completed_at=now - timedelta(days=3)))  # outside 24h
    db_session.flush()

    s = client.get("/api/v1/fleet/summary").json()
    assert s["total_hosts"] == 3
    assert s["active_hosts"] == 3
    assert s["security_updates_available"] == 1
    assert s["updates_available"] == 1
    assert s["up_to_date"] == 0  # host c has no report -> silent, not up-to-date
    assert s["reboot_required"] == 1
    assert s["silent"] == 1
    assert s["pending_updates"] == 2
    assert s["security_updates"] == 1
    assert s["jobs_succeeded_24h"] == 1
    assert s["jobs_failed_24h"] == 1
    assert s["jobs_running"] == 0


def test_fleet_summary_empty(client):
    s = client.get("/api/v1/fleet/summary").json()
    assert s["total_hosts"] == 0
    assert s["oldest_report_age_seconds"] is None
