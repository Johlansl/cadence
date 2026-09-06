from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import settings
from app.models.models import Package
from app.scheduler import (
    PACKAGES_GC_EVERY,
    _mark_packages_gc_done,
    _packages_gc_due,
    gc_orphan_packages,
    run_packages_gc_if_due,
)
from tests.conftest import bearer, create_host, pkg, report_payload

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _names(db):
    return {n for (n,) in db.execute(select(Package.name))}


def test_gc_deletes_orphan_packages_only(client, db_session):
    _, token = create_host(client)
    r = client.post(
        "/api/v1/reports",
        headers=bearer(token),
        json=report_payload(packages=[pkg("bash"), pkg("coreutils")]),
    )
    assert r.status_code == 200

    db_session.add(Package(name="ghost-pkg", architecture="amd64"))
    db_session.flush()
    assert "ghost-pkg" in _names(db_session)

    deleted = gc_orphan_packages(db_session)

    assert deleted == 1
    left = _names(db_session)
    assert "ghost-pkg" not in left
    assert {"bash", "coreutils"} <= left


def test_gc_disabled_is_a_noop(client, db_session, monkeypatch):
    _, token = create_host(client)
    client.post(
        "/api/v1/reports",
        headers=bearer(token),
        json=report_payload(packages=[pkg("bash")]),
    )
    db_session.add(Package(name="ghost-pkg", architecture="amd64"))
    db_session.flush()

    monkeypatch.setattr(settings, "packages_gc_enabled", False)
    run_packages_gc_if_due(now=NOW, db=db_session)

    assert "ghost-pkg" in _names(db_session)


def test_gc_runs_at_most_once_per_interval(client, db_session):
    _, token = create_host(client)
    client.post(
        "/api/v1/reports",
        headers=bearer(token),
        json=report_payload(packages=[pkg("bash")]),
    )

    db_session.add(Package(name="ghost-1", architecture="amd64"))
    db_session.flush()
    run_packages_gc_if_due(now=NOW, db=db_session)
    assert "ghost-1" not in _names(db_session)

    # Same window -> skipped, the second orphan survives.
    db_session.add(Package(name="ghost-2", architecture="amd64"))
    db_session.flush()
    run_packages_gc_if_due(now=NOW + timedelta(hours=1), db=db_session)
    assert "ghost-2" in _names(db_session)

    # Past the interval -> runs again.
    run_packages_gc_if_due(now=NOW + PACKAGES_GC_EVERY, db=db_session)
    assert "ghost-2" not in _names(db_session)


def test_packages_gc_due_is_persisted_across_restarts(db_session):
    assert _packages_gc_due(db_session, NOW) is True
    _mark_packages_gc_done(db_session, NOW)
    assert _packages_gc_due(db_session, NOW + timedelta(hours=1)) is False
    assert _packages_gc_due(db_session, NOW + PACKAGES_GC_EVERY) is True


def test_report_still_ingests_after_a_gc(client, db_session):
    _, token = create_host(client)
    client.post(
        "/api/v1/reports",
        headers=bearer(token),
        json=report_payload(packages=[pkg("bash")]),
    )
    gc_orphan_packages(db_session)
    r = client.post(
        "/api/v1/reports",
        headers=bearer(token),
        json=report_payload(packages=[pkg("bash"), pkg("vim", candidate="2:9.1-1")]),
    )
    assert r.status_code == 200, r.text
    assert r.json()["updates_available_count"] == 1
