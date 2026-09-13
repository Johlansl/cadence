"""Unit tests for the NVD CVSS client (`app.advisories.cve_scores`).

The NVD-side fixtures mirror the live API shape observed for CVE-2024-0727
(`cvssMetricV31` with `Primary`/`Secondary` entries carrying `cvssData`),
trimmed to the fields `select_score` reads. No test touches the network:
`fetch_nvd` is tested against a stubbed `urlopen`.
"""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timezone
from decimal import Decimal
from email.message import Message

from sqlalchemy import select

from app.advisories import cve_scores
from app.advisories.cve_scores import fetch_nvd, refresh_cve_scores, select_score
from app.models.models import CveScore

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)

_V31_DATA = {
    "version": "3.1",
    "vectorString": "CVSS:3.1/AV:L/AC:L/PR:N/UI:R/S:U/C:N/I:N/A:H",
    "baseScore": 5.5,
    "baseSeverity": "MEDIUM",
}

_V31_PAYLOAD = {
    "resultsPerPage": 1,
    "totalResults": 1,
    "vulnerabilities": [
        {
            "cve": {
                "id": "CVE-2024-0727",
                "metrics": {
                    "cvssMetricV31": [
                        {
                            "source": "nvd@nist.gov",
                            "type": "Primary",
                            "cvssData": _V31_DATA,
                        },
                        {
                            "source": "134c704f-9b21-4f2e-91b3-4a467353bcc0",
                            "type": "Secondary",
                            "cvssData": _V31_DATA,
                        },
                    ]
                },
            }
        }
    ],
}


def test_select_prefers_primary_v31():
    row = select_score(_V31_PAYLOAD, "CVE-2024-0727")
    assert row == {
        "cve_id": "CVE-2024-0727",
        "base_score": Decimal("5.5"),
        "base_severity": "MEDIUM",
        "vector": "CVSS:3.1/AV:L/AC:L/PR:N/UI:R/S:U/C:N/I:N/A:H",
        "cvss_version": "3.1",
        "source": "nvd",
    }


def test_select_falls_back_to_older_generation():
    payload = {
        "vulnerabilities": [
            {
                "cve": {
                    "id": "CVE-2020-0001",
                    "metrics": {
                        "cvssMetricV2": [
                            {
                                "source": "nvd@nist.gov",
                                "type": "Primary",
                                "cvssData": {
                                    "version": "2.0",
                                    "vectorString": "AV:N/AC:L/Au:N/C:N/I:N/A:P",
                                    "baseScore": 5.0,
                                },
                            }
                        ]
                    },
                }
            }
        ]
    }
    row = select_score(payload, "CVE-2020-0001")
    assert row["base_score"] == Decimal("5.0")
    assert row["base_severity"] is None
    assert row["cvss_version"] == "2.0"


def test_select_unknown_when_no_metrics():
    payload = {"vulnerabilities": [{"cve": {"id": "CVE-2026-0000"}}]}
    assert select_score(payload, "CVE-2026-0000") is None
    assert select_score({"vulnerabilities": []}, "CVE-2026-0000") is None
    assert select_score(_V31_PAYLOAD, "CVE-9999-0000") is None


class _StubResponse:
    def __init__(self, body: bytes):
        self._body = io.BytesIO(body)

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_sends_cve_id_and_optional_key(monkeypatch):
    seen: dict = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["apiKey"] = req.get_header("Apikey")
        return _StubResponse(json.dumps({"totalResults": 0}).encode())

    monkeypatch.setattr(cve_scores.urllib.request, "urlopen", fake_urlopen)
    fetch_nvd("CVE-2024-0727", api_key="secret-key")
    assert seen["url"].endswith("/rest/json/cves/2.0?cveId=CVE-2024-0727")
    assert seen["apiKey"] == "secret-key"

    fetch_nvd("CVE-2024-0727")
    assert seen["apiKey"] is None


def _http_error(code: int, retry_after: str | None) -> urllib.error.HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "https://services.nvd.nist.gov/rest/json/cves/2.0",
        code,
        "rate limited" if code == 429 else "not found",
        headers,
        io.BytesIO(),
    )


def test_fetch_retries_429_once_after_retry_after(monkeypatch):
    calls: list[str] = []
    slept: list[float] = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise _http_error(429, "45")
        return _StubResponse(json.dumps({"totalResults": 0}).encode())

    monkeypatch.setattr(cve_scores.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(cve_scores.time, "sleep", slept.append)
    assert fetch_nvd("CVE-2024-0727") == {"totalResults": 0}
    assert len(calls) == 2
    assert slept == [45.0]


def test_fetch_clamps_and_defaults_retry_after(monkeypatch):
    for header, expected in (("9999", 120.0), (None, 30.0), ("soon", 30.0)):
        slept: list[float] = []

        def fake_urlopen(req, timeout=None, _header=header):
            raise _http_error(429, _header)

        monkeypatch.setattr(cve_scores.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(cve_scores.time, "sleep", slept.append)
        try:
            fetch_nvd("CVE-2024-0727")
            raise AssertionError("second 429 must raise")
        except urllib.error.HTTPError as exc:
            assert exc.code == 429
        assert slept == [expected], header


def test_fetch_does_not_retry_other_errors(monkeypatch):
    calls: list[str] = []
    slept: list[float] = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        raise _http_error(404, None)

    monkeypatch.setattr(cve_scores.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(cve_scores.time, "sleep", slept.append)
    try:
        fetch_nvd("CVE-2024-0727")
        raise AssertionError("404 must raise")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    assert len(calls) == 1
    assert slept == []


def test_refresh_upserts_and_keeps_unknown_as_null(db_session):
    assert refresh_cve_scores(db_session, [select_score(_V31_PAYLOAD, "CVE-2024-0727")], NOW) == 1
    row = db_session.get(CveScore, "CVE-2024-0727")
    assert row.base_score == Decimal("5.5")
    assert row.fetched_at == NOW

    later = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)
    refresh_cve_scores(
        db_session,
        [{"cve_id": "CVE-2024-0727", "source": "nvd"}, {"cve_id": "CVE-2026-0000"}],
        later,
    )
    # Bulk upserts bypass the ORM identity map (same as refresh_advisories),
    # so expire the instances loaded above before re-reading.
    db_session.expire_all()
    rows = {r.cve_id: r for r in db_session.execute(select(CveScore)).scalars().all()}
    assert set(rows) == {"CVE-2024-0727", "CVE-2026-0000"}
    assert rows["CVE-2024-0727"].base_score is None
    assert rows["CVE-2024-0727"].fetched_at == later
