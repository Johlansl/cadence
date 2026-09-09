"""host.offline detection on the scheduler loop."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.models.models import Host, WebhookDelivery, WebhookHostState
from app.webhooks.offline import scan_offline_hosts
from tests.conftest import webhook_row

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)


def _host(db, *, hostname="vm-quiet", last_seen: datetime | None, is_active=True) -> Host:
    host = Host(
        hostname=hostname,
        os_family="debian",
        package_manager="apt",
        last_seen_at=last_seen,
        is_active=is_active,
    )
    db.add(host)
    db.flush()
    return host


def test_offline_enqueued_once_then_reset_on_report(db_session):
    webhook_row(db_session, events=("host.offline",))
    stale = NOW - timedelta(seconds=settings.webhook_offline_after_seconds + 60)
    host = _host(db_session, last_seen=stale)

    assert scan_offline_hosts(now=NOW, db=db_session) == 1
    deliveries = db_session.query(WebhookDelivery).all()
    assert len(deliveries) == 1
    body = deliveries[0].payload
    assert body["event_type"] == "host.offline"
    assert body["data"]["host_id"] == str(host.id)
    assert body["data"]["threshold_seconds"] == settings.webhook_offline_after_seconds

    state = db_session.get(WebhookHostState, host.id)
    assert state.offline_notified is True

    # second scan: still stale, but already flagged -> nothing new
    assert scan_offline_hosts(now=NOW + timedelta(minutes=5), db=db_session) == 0
    assert db_session.query(WebhookDelivery).count() == 1

    # host reports again -> flag cleared -> a later outage notifies again
    state.offline_notified = False
    db_session.flush()
    assert scan_offline_hosts(now=NOW + timedelta(hours=1), db=db_session) == 1
    assert db_session.query(WebhookDelivery).count() == 2


def test_fresh_and_inactive_hosts_are_ignored(db_session):
    webhook_row(db_session, events=("host.offline",))
    _host(db_session, hostname="vm-fresh", last_seen=NOW - timedelta(seconds=30))
    _host(db_session, hostname="vm-never", last_seen=None)
    _host(
        db_session,
        hostname="vm-retired",
        last_seen=NOW - timedelta(days=2),
        is_active=False,
    )

    assert scan_offline_hosts(now=NOW, db=db_session) == 0


def test_no_offline_subscription_means_flag_still_set_no_delivery(db_session):
    # A webhook exists but is not subscribed to host.offline.
    webhook_row(db_session, events=("job.failed",))
    stale = NOW - timedelta(seconds=settings.webhook_offline_after_seconds + 60)
    host = _host(db_session, last_seen=stale)

    scan_offline_hosts(now=NOW, db=db_session)

    assert db_session.query(WebhookDelivery).count() == 0
    # Flag is still set so we do not rescan this host every tick forever.
    assert db_session.get(WebhookHostState, host.id).offline_notified is True
