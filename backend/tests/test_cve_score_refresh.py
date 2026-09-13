"""Tests for app.scheduler.run_cve_score_refresh_if_due against a real database.

Only CVEs the dashboard can display (advisories matching a pending security
update on an active host) get a score lookup; the tens of thousands of
historical feed CVEs never enter the refresh set.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import settings
from app.models.models import Advisory, AdvisoryPackage, CveScore, Host, HostPackage, Package
from app.scheduler import (
    CVE_SCORE_REFRESH_EVERY,
    _cve_score_due,
    _mark_cve_score_done,
    run_cve_score_refresh_if_due,
)

FIXED = "3.0.14-1~deb12u2"


def _payload(cve_id: str) -> dict:
    return {
        "vulnerabilities": [
            {
                "cve": {
                    "id": cve_id,
                    "metrics": {
                        "cvssMetricV31": [
                            {
                                "source": "nvd@nist.gov",
                                "type": "Primary",
                                "cvssData": {
                                    "version": "3.1",
                                    "vectorString": (
                                        "CVSS:3.1/AV:L/AC:L/PR:N/UI:R/S:U/C:N/I:N/A:H"
                                    ),
                                    "baseScore": 5.5,
                                    "baseSeverity": "MEDIUM",
                                },
                            }
                        ]
                    },
                }
            }
        ]
    }


def _seed_host_package(
    db_session,
    *,
    hostname="vm-a",
    active=True,
    binary="libssl3",
    candidate=FIXED,
    security=True,
    os_version="12",
):
    host = Host(hostname=hostname, os_version=os_version, is_active=active)
    db_session.add(host)
    db_session.flush()
    package = Package(name=binary, architecture="amd64")
    db_session.add(package)
    db_session.flush()
    db_session.add(
        HostPackage(
            host_id=host.id,
            package_id=package.id,
            installed_version="3.0.11",
            candidate_version=candidate,
            is_security_update=security,
        )
    )
    db_session.flush()
    return host


def _seed_advisory(db_session, adv_id="DSA-5745-1", cves=None, fixed=FIXED):
    db_session.add(
        Advisory(
            id=adv_id,
            source="debian-dsa",
            url=f"https://security-tracker.debian.org/tracker/{adv_id}",
            title="openssl - security update",
            cve_ids=cves if cves is not None else ["CVE-2024-0727"],
            published_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        )
    )
    db_session.add(
        AdvisoryPackage(
            advisory_id=adv_id,
            release="bookworm",
            package="openssl",
            fixed_version=fixed,
        )
    )
    db_session.flush()


def test_cve_score_due_persisted_across_restarts(db_session):
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    assert _cve_score_due(db_session, now) is True
    _mark_cve_score_done(db_session, now)
    assert _cve_score_due(db_session, now + timedelta(hours=1)) is False
    assert _cve_score_due(db_session, now + CVE_SCORE_REFRESH_EVERY) is True


def test_refresh_scores_only_displayed_cves(db_session, monkeypatch):
    monkeypatch.setattr(settings, "cve_score_refresh_enabled", True)
    monkeypatch.setattr(settings, "cve_nvd_api_key", "")
    seen: list[str] = []

    def fake_fetch(cve_id, **kw):
        seen.append(cve_id)
        assert kw.get("api_key") is None
        return _payload(cve_id)

    monkeypatch.setattr("app.scheduler.fetch_nvd", fake_fetch)
    _seed_host_package(db_session)
    _seed_advisory(db_session, cves=["CVE-2024-0727", "CVE-2024-0728"])
    # Linked to nothing pending: a different fixed version never matches.
    _seed_advisory(db_session, adv_id="DSA-9999-1", cves=["CVE-2024-0999"], fixed="0.0")
    now = datetime.now(timezone.utc)

    run_cve_score_refresh_if_due(now=now, db=db_session)

    assert seen == ["CVE-2024-0727", "CVE-2024-0728"]
    assert db_session.get(CveScore, "CVE-2024-0727").base_severity == "MEDIUM"
    assert db_session.get(CveScore, "CVE-2024-0999") is None
    assert _cve_score_due(db_session, now) is False


def test_refresh_ignores_inactive_hosts_and_non_security_rows(db_session, monkeypatch):
    monkeypatch.setattr(settings, "cve_score_refresh_enabled", True)
    calls: list[str] = []
    monkeypatch.setattr(
        "app.scheduler.fetch_nvd", lambda cve_id, **kw: calls.append(cve_id) or {}
    )
    _seed_host_package(db_session, hostname="retired", active=False)
    _seed_host_package(db_session, hostname="plain", binary="vim", security=False)
    _seed_advisory(db_session)

    run_cve_score_refresh_if_due(now=datetime.now(timezone.utc), db=db_session)

    assert calls == []
    assert db_session.execute(select(CveScore)).scalars().all() == []


def test_refresh_stores_unknown_displayed_cve_as_null(db_session, monkeypatch):
    monkeypatch.setattr(settings, "cve_score_refresh_enabled", True)
    monkeypatch.setattr(
        "app.scheduler.fetch_nvd", lambda cve_id, **kw: {"vulnerabilities": []}
    )
    _seed_host_package(db_session)
    _seed_advisory(db_session, cves=["CVE-2026-0000"])

    run_cve_score_refresh_if_due(now=datetime.now(timezone.utc), db=db_session)

    row = db_session.get(CveScore, "CVE-2026-0000")
    assert row is not None
    assert row.base_score is None
    assert row.base_severity is None


def test_refresh_keeps_state_key_on_fetch_failure(db_session, monkeypatch):
    monkeypatch.setattr(settings, "cve_score_refresh_enabled", True)

    def boom(cve_id, **kw):
        raise OSError("network down")

    monkeypatch.setattr("app.scheduler.fetch_nvd", boom)
    _seed_host_package(db_session)
    _seed_advisory(db_session)
    now = datetime.now(timezone.utc)

    run_cve_score_refresh_if_due(now=now, db=db_session)

    assert _cve_score_due(db_session, now) is True  # still due -> next tick retries
    assert db_session.execute(select(CveScore)).scalars().all() == []


def test_refresh_skips_when_recent(db_session, monkeypatch):
    monkeypatch.setattr(settings, "cve_score_refresh_enabled", True)
    calls: list[str] = []
    monkeypatch.setattr(
        "app.scheduler.fetch_nvd", lambda cve_id, **kw: calls.append(cve_id) or {}
    )
    _seed_host_package(db_session)
    _seed_advisory(db_session)
    now = datetime.now(timezone.utc)
    _mark_cve_score_done(db_session, now)

    run_cve_score_refresh_if_due(now=now + timedelta(hours=1), db=db_session)

    assert calls == []


def test_refresh_disabled_is_a_noop(db_session, monkeypatch):
    monkeypatch.setattr(settings, "cve_score_refresh_enabled", False)

    def boom(cve_id, **kw):
        raise AssertionError("must not fetch when the feature is disabled")

    monkeypatch.setattr("app.scheduler.fetch_nvd", boom)
    _seed_host_package(db_session)
    _seed_advisory(db_session)

    run_cve_score_refresh_if_due(now=datetime.now(timezone.utc), db=db_session)

    assert db_session.execute(select(CveScore)).scalars().all() == []


def test_refresh_paces_lookups(db_session, monkeypatch):
    import app.scheduler as scheduler_mod

    monkeypatch.setattr(settings, "cve_score_refresh_enabled", True)
    monkeypatch.setattr(
        "app.scheduler.fetch_nvd", lambda cve_id, **kw: {"vulnerabilities": []}
    )
    slept: list[float] = []
    monkeypatch.setattr(scheduler_mod.time, "sleep", slept.append)
    _seed_host_package(db_session)
    _seed_advisory(db_session, cves=["CVE-2024-0001", "CVE-2024-0002"])

    run_cve_score_refresh_if_due(now=datetime.now(timezone.utc), db=db_session)

    assert slept == [scheduler_mod.NVD_REQUEST_SPACING_SECONDS]


def test_historical_feed_cves_never_enter_the_refresh_set(db_session, monkeypatch):
    """Regression test for the 32k-CVE crawl: an advisory whose CVEs match
    nothing pending must cost zero NVD requests, however many CVEs it lists."""
    monkeypatch.setattr(settings, "cve_score_refresh_enabled", True)

    def boom(cve_id, **kw):
        raise AssertionError(f"must not fetch {cve_id}")

    monkeypatch.setattr("app.scheduler.fetch_nvd", boom)
    _seed_advisory(
        db_session, cves=[f"CVE-2020-{i:04d}" for i in range(50)], fixed="0.0"
    )
    now = datetime.now(timezone.utc)

    run_cve_score_refresh_if_due(now=now, db=db_session)

    assert _cve_score_due(db_session, now) is False
    assert db_session.execute(select(CveScore)).scalars().all() == []
