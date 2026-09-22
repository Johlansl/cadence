"""Self-monitoring contract (docs/decisions.md "Monitoring the control plane").

/api/v1/metrics exposes Prometheus-text gauges computed by app.metrics over
small/bounded tables only. These tests pin the format, the bucketing and the
degraded behaviour (200 + cadence_db_up 0 when the database is down).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import OperationalError

from app.models.models import Campaign, Host, Job, SchedulerState, WebhookDelivery
from tests.conftest import create_host, webhook_row


def test_metrics_exposition_and_buckets(client, db_session):
    host_id, _token = create_host(client)
    other_id, _token2 = create_host(client, hostname="vm-second")
    host = db_session.get(Host, host_id)
    host.agent_version = "0.14.0"
    host.last_seen_at = datetime.now(timezone.utc)
    other = db_session.get(Host, other_id)
    other.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    # One active job per host (ux_jobs_one_active_per_host): spread the
    # active rows across the two hosts; terminal rows are unconstrained.
    db_session.add_all(
        [
            Job(host_id=host_id, status="pending", job_type="apt_upgrade"),
            Job(host_id=other_id, status="running", job_type="reboot"),
            Job(
                host_id=host_id,
                status="failed",
                job_type="apt_upgrade",
                completed_at=datetime.now(timezone.utc),
            ),
            Campaign(
                name="m",
                stages=[1],
                max_concurrency=1,
                max_failures=1,
                observation_window_seconds=0,
                status="running",
            ),
        ]
    )
    hook = webhook_row(db_session)
    db_session.add(
        WebhookDelivery(
            webhook_id=hook.id,
            event_type="job.failed",
            payload={},
            status="pending",
        )
    )
    db_session.add(
        SchedulerState(key="last_tick_at", value=datetime.now(timezone.utc).isoformat())
    )
    db_session.commit()

    r = client.get("/api/v1/metrics")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert 'cadence_jobs{job_type="apt_upgrade",status="pending"} 1.0' in body
    assert 'cadence_jobs{job_type="reboot",status="running"} 1.0' in body
    assert 'cadence_jobs_failed_24h{job_type="apt_upgrade"} 1.0' in body
    assert 'cadence_campaigns{status="running"} 1.0' in body
    assert "cadence_webhook_deliveries_pending 1.0" in body
    assert 'cadence_hosts{state="ok"} 1.0' in body
    assert 'cadence_hosts{state="silent"} 1.0' in body
    assert 'cadence_agent_versions{version="0.14.0"} 1.0' in body
    assert "cadence_db_up 1.0" in body
    assert "cadence_scheduler_heartbeat_age_seconds" in body


def test_metrics_db_down_stays_200_with_db_up_zero(client):
    # Real DB failure mode: the session is handed out fine, the first query
    # raises (engine.connect fails lazily). A dead session, not a dead dep.
    from unittest.mock import MagicMock

    from sqlalchemy.orm import Session as SASession

    from app.api.deps import get_db
    from app.main import app

    dead = MagicMock(spec=SASession)
    dead.execute.side_effect = OperationalError(
        "SELECT 1", {}, Exception("db gone")
    )
    app.dependency_overrides[get_db] = lambda: dead
    try:
        r = client.get("/api/v1/metrics")
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert r.status_code == 200, r.text
    assert r.text.strip().endswith("cadence_db_up 0")


def test_metrics_rejects_tampered_format_guard(client):
    # The exposition must stay parseable: every gauge has HELP/TYPE lines.
    r = client.get("/api/v1/metrics")
    assert r.status_code == 200
    names = [
        line.split()[2]
        for line in r.text.splitlines()
        if line.startswith("# TYPE ")
    ]
    assert "cadence_db_up" in names
    assert "cadence_jobs" in names
    for name in names:
        assert f"# HELP {name} " in r.text
