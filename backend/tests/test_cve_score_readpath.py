"""GET /hosts/{id} and GET /packages attach the cached CVSS rollup to linked
advisories, without changing linkage or any security count."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.models.models import Advisory, AdvisoryPackage, CveScore
from tests.conftest import create_host, pkg, report_payload, signed

FIXED = "3.0.14-1~deb12u2"
DSA_URL = "https://security-tracker.debian.org/tracker/DSA-5745-1"


def _seed(db_session):
    db_session.add(
        Advisory(
            id="DSA-5745-1",
            source="debian-dsa",
            url=DSA_URL,
            title="openssl - security update",
            cve_ids=["CVE-2024-0727", "CVE-2026-0000"],
            published_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
        )
    )
    db_session.add(
        AdvisoryPackage(
            advisory_id="DSA-5745-1",
            release="bookworm",
            package="openssl",
            fixed_version=FIXED,
        )
    )
    # Highest known score wins the rollup; the NULL row stays neutral.
    db_session.add(
        CveScore(
            cve_id="CVE-2024-0727",
            base_score=Decimal("5.5"),
            base_severity="MEDIUM",
            vector="CVSS:3.1/AV:L/AC:L/PR:N/UI:R/S:U/C:N/I:N/A:H",
            cvss_version="3.1",
            fetched_at=datetime(2026, 9, 13, tzinfo=timezone.utc),
        )
    )
    db_session.add(CveScore(cve_id="CVE-2026-0000"))
    db_session.flush()


def _report(client, token, packages):
    r = client.post(
        "/api/v1/reports",
        auth=signed(token),
        json=report_payload(hostname="vm-a", os_version="12", packages=packages),
    )
    assert r.status_code == 200, r.text


def test_host_detail_attaches_cvss_rollup(client, db_session):
    host_id, token = create_host(client, hostname="vm-a")
    _seed(db_session)
    _report(client, token, [pkg("libssl3", candidate=FIXED, security=True)])

    body = client.get(f"/api/v1/hosts/{host_id}").json()
    assert body["packages"][0]["advisories"] == [
        {
            "id": "DSA-5745-1",
            "url": DSA_URL,
            "cves": ["CVE-2024-0727", "CVE-2026-0000"],
            "cvss_score": 5.5,
            "cvss_severity": "MEDIUM",
            "cvss_vector": "CVSS:3.1/AV:L/AC:L/PR:N/UI:R/S:U/C:N/I:N/A:H",
        }
    ]
    assert body["security_updates_count"] == 1
    assert body["updates_available_count"] == 1


def test_packages_view_attaches_cvss_rollup(client, db_session):
    _, token = create_host(client, hostname="vm-a")
    _seed(db_session)
    _report(client, token, [pkg("libssl3", candidate=FIXED, security=True)])

    rows = client.get("/api/v1/packages?status=security").json()
    libssl = next(r for r in rows if r["name"] == "libssl3")
    assert libssl["hosts"][0]["advisories"][0]["cvss_score"] == 5.5
    assert libssl["hosts"][0]["advisories"][0]["cvss_severity"] == "MEDIUM"
