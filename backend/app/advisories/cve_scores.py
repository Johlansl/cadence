"""CVSS score enrichment for CVEs already linked via DSA/DLA advisories.

Roadmap item 9. The DSA/DLA pipeline stays the source of truth for which
CVEs concern which package; this module only resolves a cached CVSS score
per CVE from the NVD CVE API 2.0 so the read API never fetches from the
network. Pure fetch/parse plus a small upsert; the scheduler wires this to
`app.scheduler` (the refresh step) and the read API joins `cve_scores`.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.models import CveScore

log = logging.getLogger("cadence.cve_scores")


def _f(**fields: object) -> dict:
    """Wrap structured fields for the logfmt formatter."""
    return {"fields": fields}

NVD_CVES_URL = "https://services.nvd.nist.gov/rest/json/cves/2.0"

# Upper bound for honouring a 429 Retry-After: a single score lookup must
# never stall the 6 h scheduler tick for longer than this.
NVD_RETRY_AFTER_MAX_SECONDS = 120.0

# Fallback wait when a 429 carries no usable Retry-After header.
NVD_RETRY_AFTER_DEFAULT_SECONDS = 30.0

# NVD metric blocks in preference order: the API serves several CVSS
# generations side by side and not every CVE carries every generation.
_METRIC_BLOCKS = ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2")


def _retry_after_delay(exc: urllib.error.HTTPError) -> float:
    """Seconds to wait before retrying a 429, from its `Retry-After` header
    (numeric form) clamped to `NVD_RETRY_AFTER_MAX_SECONDS`. A missing or
    unparsable header falls back to `NVD_RETRY_AFTER_DEFAULT_SECONDS`."""
    raw = None
    try:
        raw = exc.headers.get("Retry-After")
    except AttributeError:
        raw = None
    try:
        delay = float(raw) if raw is not None else NVD_RETRY_AFTER_DEFAULT_SECONDS
    except (TypeError, ValueError):
        delay = NVD_RETRY_AFTER_DEFAULT_SECONDS
    return max(0.0, min(delay, NVD_RETRY_AFTER_MAX_SECONDS))


def _get_once(
    url: str, headers: dict[str, str], timeout: float, max_bytes: int
) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers)
    chunks: list[bytes] = []
    total = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"{url}: response exceeds {max_bytes} bytes")
            chunks.append(chunk)
    return json.loads(b"".join(chunks).decode("utf-8"))


def fetch_nvd(
    cve_id: str,
    *,
    api_key: str | None = None,
    timeout: float = 30.0,
    max_bytes: int = 256 * 1024,
) -> dict[str, Any]:
    """GET one CVE record from the NVD CVE API 2.0, refusing a body larger
    than `max_bytes`. A single record is a few KB; hitting the cap means the
    endpoint changed shape and the caller must adapt on purpose. The optional
    `api_key` is sent as the documented `apiKey` header and is never logged.
    A 429 is retried exactly once after its `Retry-After` wait (capped);
    anything else raises for the caller to handle.
    """
    query = urllib.parse.urlencode({"cveId": cve_id})
    url = f"{NVD_CVES_URL}?{query}"
    headers = {"User-Agent": "cadence-scheduler"}
    if api_key:
        headers["apiKey"] = api_key
    try:
        return _get_once(url, headers, timeout, max_bytes)
    except urllib.error.HTTPError as exc:
        if exc.code != 429:
            raise
        delay = _retry_after_delay(exc)
        log.info(
            "nvd rate limited, retrying once",
            extra=_f(cve_id=cve_id, retry_after_s=delay),
        )
        time.sleep(delay)
        return _get_once(url, headers, timeout, max_bytes)


def select_score(payload: dict[str, Any], cve_id: str) -> dict[str, Any] | None:
    """Pick the CVSS score for `cve_id` out of a decoded NVD response.
    Returns a `cve_scores` row dict, or None when the NVD carries no metrics
    for this CVE (unknown, never zero). Prefers the `Primary` entry of the
    newest CVSS generation available."""
    for entry in payload.get("vulnerabilities", []):
        cve = entry.get("cve", {})
        if cve.get("id") != cve_id:
            continue
        metrics = cve.get("metrics", {})
        for block in _METRIC_BLOCKS:
            entries = metrics.get(block)
            if not entries:
                continue
            primary = [e for e in entries if e.get("type") == "Primary"]
            data = (primary[0] if primary else entries[0]).get("cvssData", {})
            if "baseScore" not in data:
                continue
            return {
                "cve_id": cve_id,
                "base_score": Decimal(str(data["baseScore"])),
                "base_severity": data.get("baseSeverity"),
                "vector": data.get("vectorString"),
                "cvss_version": data.get("version"),
                "source": "nvd",
            }
        return None
    return None


def refresh_cve_scores(
    db: Session, rows: list[dict[str, Any]], now: datetime
) -> int:
    """Upsert one `cve_scores` row per entry in `rows` (as returned by
    `select_score`; a row with NULL score records a checked-but-unknown CVE).
    Returns the row count. One commit for the whole batch."""
    for row in rows:
        fields = {
            "base_score": row.get("base_score"),
            "base_severity": row.get("base_severity"),
            "vector": row.get("vector"),
            "cvss_version": row.get("cvss_version"),
            "source": row.get("source", "nvd"),
            "fetched_at": now,
        }
        db.execute(
            pg_insert(CveScore)
            .values(cve_id=row["cve_id"], **fields)
            .on_conflict_do_update(index_elements=["cve_id"], set_=fields)
        )
    db.commit()
    return len(rows)
