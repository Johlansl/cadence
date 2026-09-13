"""Tests for app.scheduler.run_cve_score_refresh_if_due against a real database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.config import settings
from app.models.models import Advisory, CveScore
from app.scheduler import (
    CVE_SCORE_REFRESH_EVERY,
    _cve_score_due,
    _mark_cve_score_done,
    run_cve_score_refresh_if_due,
)

_V31_PAYLOAD = {
    "vulnerabilities": [
        {
            "cve": {
                "id": "CVE-2024-0727",
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
    ],
}


def _seed_advisory(db_session, adv_id="DSA-5745-1", cves=None):
    db_session.add(
        Advisory(
            id=adv_id,
            source="debian-dsa",
            url=f"https://security-tracker.debian.org/tracker/{adv_id}",
            title="security update",
            cve_ids=cves if cves is not None else ["CVE-2024-0727"],
            published_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        )
    )
    db_session.flush()


def test_cve_score_due_persisted_across_restarts(db_session):
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    assert _cve_score_due(db_session, now) is True
    _mark_cve_score_done(db_session, now)
    assert _cve_score_due(db_session, now + timedelta(hours=1)) is False
    assert _cve_score_due(db_session, now + CVE_SCORE_REFRESH_EVERY) is True


def test_refresh_scores_only_referenced_cves(db_session, monkeypatch):
    monkeypatch.setattr(settings, "cve_score_refresh_enabled", True)
    monkeypatch.setattr(settings, "cve_nvd_api_key", "")
    seen: list[str] = []

    def fake_fetch(cve_id, **kw):
        seen.append(cve_id)
        assert kw.get("api_key") is None
        return _V31_PAYLOAD

    monkeypatch.setattr("app.scheduler.fetch_nvd", fake_fetch)
    _seed_advisory(db_session, cves=["CVE-2024-0727", "CVE-2024-0727"])
    _seed_advisory(db_session, adv_id="DLA-1-1", cves=[])
    now = datetime.now(timezone.utc)

    run_cve_score_refresh_if_due(now=now, db=db_session)

    assert seen == ["CVE-2024-0727"]
    row = db_session.get(CveScore, "CVE-2024-0727")
    assert str(row.base_score) == "5.5"
    assert row.base_severity == "MEDIUM"
    assert _cve_score_due(db_session, now) is False


def test_refresh_stores_unknown_cve_as_null(db_session, monkeypatch):
    monkeypatch.setattr(settings, "cve_score_refresh_enabled", True)
    monkeypatch.setattr(
        "app.scheduler.fetch_nvd", lambda cve_id, **kw: {"vulnerabilities": []}
    )
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
    _seed_advisory(db_session)
    now = datetime.now(timezone.utc)
    _mark_cve_score_done(db_session, now)

    run_cve_score_refresh_if_due(now=now + timedelta(hours=1), db=db_session)

    assert calls == []


def test_refresh_paces_lookups_without_slowing_a_single_cve(db_session, monkeypatch):
    import app.scheduler as scheduler_mod

    monkeypatch.setattr(settings, "cve_score_refresh_enabled", True)
    monkeypatch.setattr(
        "app.scheduler.fetch_nvd", lambda cve_id, **kw: {"vulnerabilities": []}
    )
    slept: list[float] = []
    monkeypatch.setattr(scheduler_mod.time, "sleep", slept.append)
    _seed_advisory(db_session, cves=["CVE-2024-0001", "CVE-2024-0002"])

    run_cve_score_refresh_if_due(now=datetime.now(timezone.utc), db=db_session)

    assert slept == [scheduler_mod.NVD_REQUEST_SPACING_SECONDS]


def test_refresh_disabled_is_a_noop(db_session, monkeypatch):
    monkeypatch.setattr(settings, "cve_score_refresh_enabled", False)

    def boom(cve_id, **kw):
        raise AssertionError("must not fetch when the feature is disabled")

    monkeypatch.setattr("app.scheduler.fetch_nvd", boom)
    _seed_advisory(db_session)

    run_cve_score_refresh_if_due(now=datetime.now(timezone.utc), db=db_session)

    assert db_session.execute(select(CveScore)).scalars().all() == []
