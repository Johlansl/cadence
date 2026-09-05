import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from app.models.models import Host, Job
from tests.conftest import bearer, create_host, pkg, report_payload


def _report(client, token, **kw):
    r = client.post("/api/v1/reports", headers=bearer(token), json=report_payload(**kw))
    assert r.status_code == 200, r.text


def _set_last_seen(db_session, host_id: str, when: datetime) -> None:
    db_session.execute(
        update(Host).where(Host.id == uuid.UUID(host_id)).values(last_seen_at=when)
    )
    db_session.commit()


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


def test_fleet_summary_late_bucket(client, db_session):
    h, t = create_host(client, hostname="a")
    _report(client, t, packages=[pkg("bash")])
    _set_last_seen(db_session, h, datetime.now(timezone.utc) - timedelta(minutes=8))

    s = client.get("/api/v1/fleet/summary").json()
    assert s["late"] == 1
    assert s["silent"] == 0
    assert s["up_to_date"] == 1  # still classified by its (zero) updates


def test_fleet_summary_silent_by_age_still_classified_by_updates(client, db_session):
    h, t = create_host(client, hostname="a")
    _report(client, t, packages=[pkg("bash")])  # no pending updates
    _set_last_seen(db_session, h, datetime.now(timezone.utc) - timedelta(minutes=30))

    s = client.get("/api/v1/fleet/summary").json()
    # A silent-by-age host is counted in BOTH silent and up_to_date -- the
    # never-reported host in test_fleet_summary_aggregates is the only one
    # excluded from the status buckets.
    assert s["silent"] == 1
    assert s["up_to_date"] == 1


def test_fleet_summary_inactive_hosts_excluded_from_active_counts(client, db_session):
    h, t = create_host(client, hostname="a")
    _report(
        client, t,
        packages=[pkg("openssl", candidate="3.1", security=True)],
        reboot_required=True,
    )
    db_session.execute(update(Host).where(Host.id == uuid.UUID(h)).values(is_active=False))
    db_session.commit()

    s = client.get("/api/v1/fleet/summary").json()
    assert s["total_hosts"] == 1
    assert s["active_hosts"] == 0
    assert s["inactive_hosts"] == 1
    assert s["security_updates_available"] == 0
    assert s["reboot_required"] == 0
    assert s["pending_updates"] == 0
    assert s["silent"] == 0


def test_fleet_summary_oldest_age_is_the_older_active_report(client, db_session):
    h1, t1 = create_host(client, hostname="a")
    h2, t2 = create_host(client, hostname="b")
    _report(client, t1, packages=[])
    _report(client, t2, packages=[])
    _set_last_seen(db_session, h1, datetime.now(timezone.utc) - timedelta(minutes=40))
    _set_last_seen(db_session, h2, datetime.now(timezone.utc) - timedelta(minutes=2))

    age = client.get("/api/v1/fleet/summary").json()["oldest_report_age_seconds"]
    assert 40 * 60 - 30 <= age <= 40 * 60 + 30
