"""GET /hosts/{id} and GET /packages surface linked Debian advisories for
apt-flagged security updates, without changing any security count."""

from __future__ import annotations

from datetime import datetime, timezone

from app.models.models import Advisory, AdvisoryPackage
from tests.conftest import bearer, create_host, pkg, report_payload

FIXED = "3.0.14-1~deb12u2"
DSA_URL = "https://security-tracker.debian.org/tracker/DSA-5745-1"


def _seed_openssl_dsa(db):
    db.add(
        Advisory(
            id="DSA-5745-1",
            source="debian-dsa",
            url=DSA_URL,
            title="openssl - security update",
            cve_ids=["CVE-2026-6119"],
            published_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        )
    )
    db.add(
        AdvisoryPackage(
            advisory_id="DSA-5745-1",
            release="bookworm",
            package="openssl",
            fixed_version=FIXED,
        )
    )
    db.flush()


def _report(client, token, packages, *, os_version="12"):
    r = client.post(
        "/api/v1/reports",
        headers=bearer(token),
        json=report_payload(hostname="vm-a", os_version=os_version, packages=packages),
    )
    assert r.status_code == 200, r.text


def test_host_detail_links_matching_advisory_and_leaves_counts(client, db_session):
    host_id, token = create_host(client, hostname="vm-a")
    _seed_openssl_dsa(db_session)
    _report(
        client,
        token,
        [
            pkg("libssl3", candidate=FIXED, security=True),
            pkg("vim", candidate="2:9.1-1", security=False),
            pkg("bash", candidate="5.2-3", security=True),  # security, no advisory
        ],
    )

    body = client.get(f"/api/v1/hosts/{host_id}").json()
    by_name = {p["name"]: p for p in body["packages"]}

    assert by_name["libssl3"]["advisories"] == [
        {"id": "DSA-5745-1", "url": DSA_URL, "cves": ["CVE-2026-6119"]}
    ]
    assert by_name["vim"]["advisories"] == []
    assert by_name["bash"]["advisories"] == []
    assert body["security_updates_count"] == 2
    assert body["updates_available_count"] == 3
    assert body["status"] == "security_updates_available"


def test_counts_identical_without_any_advisory_rows(client, db_session):
    host_id, token = create_host(client, hostname="vm-a")
    _report(client, token, [pkg("libssl3", candidate=FIXED, security=True)])

    body = client.get(f"/api/v1/hosts/{host_id}").json()
    assert body["packages"][0]["advisories"] == []
    assert body["security_updates_count"] == 1
    assert body["updates_available_count"] == 1


def test_no_match_when_candidate_version_differs(client, db_session):
    host_id, token = create_host(client, hostname="vm-a")
    _seed_openssl_dsa(db_session)
    _report(client, token, [pkg("libssl3", candidate="3.0.13-1~deb12u1", security=True)])

    body = client.get(f"/api/v1/hosts/{host_id}").json()
    assert body["packages"][0]["advisories"] == []


def test_no_match_for_unknown_release(client, db_session):
    host_id, token = create_host(client, hostname="vm-a")
    _seed_openssl_dsa(db_session)
    _report(
        client,
        token,
        [pkg("libssl3", candidate=FIXED, security=True)],
        os_version="99",
    )

    body = client.get(f"/api/v1/hosts/{host_id}").json()
    assert body["packages"][0]["advisories"] == []


def test_packages_view_links_advisory(client, db_session):
    _, token = create_host(client, hostname="vm-a")
    _seed_openssl_dsa(db_session)
    _report(client, token, [pkg("libssl3", candidate=FIXED, security=True)])

    rows = client.get("/api/v1/packages?status=security").json()
    libssl = next(r for r in rows if r["name"] == "libssl3")
    assert libssl["hosts"][0]["advisories"] == [
        {"id": "DSA-5745-1", "url": DSA_URL, "cves": ["CVE-2026-6119"]}
    ]
