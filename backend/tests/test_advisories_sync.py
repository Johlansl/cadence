"""Tests for app.advisories.sync.refresh_advisories against a real database."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.advisories.debian import ParsedAdvisory
from app.advisories.sync import refresh_advisories
from app.models.models import Advisory, AdvisoryPackage

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _adv(adv_id: str = "DSA-1000-1", *, cves=None, packages=None):
    return ParsedAdvisory(
        id=adv_id,
        source="debian-dsa",
        url=f"https://security-tracker.debian.org/tracker/{adv_id}",
        title="security update",
        cve_ids=cves if cves is not None else ["CVE-2026-0001"],
        published_at=NOW,
        packages=packages
        if packages is not None
        else [("bookworm", "openssl", "3.0.14-1~deb12u2")],
    )


def test_inserts_advisory_and_package_rows(db_session):
    counts = refresh_advisories(db_session, [_adv()], NOW)
    assert counts == {"advisories": 1, "advisory_packages": 1}

    row = db_session.get(Advisory, "DSA-1000-1")
    assert row.source == "debian-dsa"
    assert row.cve_ids == ["CVE-2026-0001"]
    pkg = db_session.execute(select(AdvisoryPackage)).scalars().one()
    assert (pkg.release, pkg.package, pkg.fixed_version) == (
        "bookworm",
        "openssl",
        "3.0.14-1~deb12u2",
    )


def test_rerun_identical_is_idempotent_and_bumps_updated_at(db_session):
    refresh_advisories(db_session, [_adv()], NOW)
    later = NOW + timedelta(hours=6)
    counts = refresh_advisories(db_session, [_adv()], later)

    assert counts == {"advisories": 1, "advisory_packages": 1}
    advs = db_session.execute(select(Advisory)).scalars().all()
    assert len(advs) == 1
    assert advs[0].updated_at == later
    assert len(db_session.execute(select(AdvisoryPackage)).scalars().all()) == 1


def test_changed_fixed_version_replaces_the_row(db_session):
    refresh_advisories(db_session, [_adv()], NOW)
    refresh_advisories(
        db_session,
        [_adv(packages=[("bookworm", "openssl", "3.0.15-1~deb12u1")])],
        NOW + timedelta(hours=6),
    )
    pkg = db_session.execute(select(AdvisoryPackage)).scalars().one()
    assert pkg.fixed_version == "3.0.15-1~deb12u1"


def test_dropped_release_is_removed(db_session):
    refresh_advisories(
        db_session,
        [
            _adv(
                packages=[
                    ("bookworm", "openssl", "3.0.14-1~deb12u2"),
                    ("trixie", "openssl", "3.3.1-2"),
                ]
            )
        ],
        NOW,
    )
    refresh_advisories(
        db_session,
        [_adv(packages=[("bookworm", "openssl", "3.0.14-1~deb12u2")])],
        NOW + timedelta(hours=6),
    )
    releases = db_session.execute(select(AdvisoryPackage.release)).scalars().all()
    assert releases == ["bookworm"]


def test_advisory_missing_from_a_later_batch_is_kept(db_session):
    refresh_advisories(db_session, [_adv("DSA-1000-1"), _adv("DSA-1001-1")], NOW)
    refresh_advisories(db_session, [_adv("DSA-1001-1")], NOW + timedelta(hours=6))
    ids = set(db_session.execute(select(Advisory.id)).scalars().all())
    assert ids == {"DSA-1000-1", "DSA-1001-1"}


def test_advisory_with_no_packages(db_session):
    counts = refresh_advisories(db_session, [_adv(cves=[], packages=[])], NOW)
    assert counts == {"advisories": 1, "advisory_packages": 0}
    assert db_session.get(Advisory, "DSA-1000-1").cve_ids == []
