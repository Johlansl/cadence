"""Keyset pagination on the history endpoints.

Regression cover for the missing tiebreaker: with a plain ``received_at <
before`` / ``created_at < before`` cursor, a page boundary that lands in
the middle of a group of rows sharing a timestamp drops the rows left on
the far side of the cut. Passing ``before_id`` alongside ``before`` adds
the id tiebreaker so every row is returned exactly once.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.models.models import Job, Report
from tests.conftest import create_host

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
# 1 + 1 + 3 (shared timestamp) + 1 = 6 rows, so limit=2 pages cut the group.
STAMPS = [
    T0,
    T0 + timedelta(minutes=1),
    T0 + timedelta(minutes=2),
    T0 + timedelta(minutes=2),
    T0 + timedelta(minutes=2),
    T0 + timedelta(minutes=3),
]


def _page_through(client, base_url: str) -> list:
    """Walk every page at limit=2 using the (before, before_id) cursor."""
    seen: list = []
    url = f"{base_url}?limit=2"
    for _ in range(20):  # safety stop
        page = client.get(url).json()
        seen.extend(row["id"] for row in page)
        if len(page) < 2:
            break
        last = page[-1]
        url = (
            f"{base_url}?limit=2"
            f"&before={last['received_at' if 'received_at' in last else 'created_at']}"
            f"&before_id={last['id']}"
        )
    return seen


def test_reports_keyset_returns_every_row_once(client, db_session):
    hid, _ = create_host(client)
    for ts in STAMPS:
        db_session.add(Report(host_id=uuid.UUID(hid), received_at=ts, raw_payload={}))
    db_session.commit()

    paged = _page_through(client, f"/api/v1/hosts/{hid}/reports")
    full = [r["id"] for r in client.get(f"/api/v1/hosts/{hid}/reports?limit=500").json()]

    assert len(full) == 6
    assert paged == full  # same set, same order, no gaps, no repeats
    assert len(set(paged)) == 6


def test_jobs_keyset_returns_every_row_once(client, db_session):
    hid, _ = create_host(client)
    for ts in STAMPS:
        db_session.add(Job(host_id=uuid.UUID(hid), created_at=ts))
    db_session.commit()

    paged = _page_through(client, f"/api/v1/hosts/{hid}/jobs")
    full = [j["id"] for j in client.get(f"/api/v1/hosts/{hid}/jobs?limit=200").json()]

    assert len(full) == 6
    assert paged == full
    assert len(set(paged)) == 6


def test_before_without_before_id_keeps_strict_timestamp_behaviour(client, db_session):
    hid, _ = create_host(client)
    for ts in STAMPS:
        db_session.add(Report(host_id=uuid.UUID(hid), received_at=ts, raw_payload={}))
    db_session.commit()

    cut = (T0 + timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
    older = client.get(f"/api/v1/hosts/{hid}/reports?before={cut}").json()

    def _parse(s: str) -> datetime:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    # Only the two rows strictly older than T0+2m; the shared-timestamp group
    # at exactly T0+2m is excluded, as before.
    assert [_parse(r["received_at"]) for r in older] == [T0 + timedelta(minutes=1), T0]
