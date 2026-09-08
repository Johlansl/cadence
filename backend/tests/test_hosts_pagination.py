"""Keyset pagination and server-side filters on GET /api/v1/hosts.

The list is ordered by (hostname, id); the id tiebreaker keeps hosts that
share a hostname from being split across a page boundary.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from app.models.models import Host
from tests.conftest import create_host, pkg, report_payload, signed


def _report(client, token, hostname, **kw):
    # a report also syncs Host.hostname from the payload -- pass the real name
    r = client.post(
        "/api/v1/reports", auth=signed(token), json=report_payload(hostname=hostname, **kw)
    )
    assert r.status_code == 200, r.text


def _page_through(client, limit=2, **params) -> list[dict]:
    seen: list[dict] = []
    qs = {"limit": limit, **params}
    for _ in range(50):  # safety stop
        page = client.get("/api/v1/hosts", params=qs).json()
        seen.extend(page)
        if len(page) < limit:
            break
        last = page[-1]
        qs = {"limit": limit, "after": last["hostname"], "after_id": last["id"], **params}
    return seen


def test_no_params_returns_the_full_list_ordered_by_hostname(client):
    for name in ("charlie", "alice", "bob"):
        create_host(client, hostname=name)
    names = [h["hostname"] for h in client.get("/api/v1/hosts").json()]
    assert names == ["alice", "bob", "charlie"]


def test_keyset_walks_every_row_once_with_duplicate_hostnames(client):
    for name in ("a", "b", "b", "b", "c"):
        create_host(client, hostname=name)

    paged = [h["id"] for h in _page_through(client, limit=2)]
    full = [h["id"] for h in client.get("/api/v1/hosts").json()]

    assert len(full) == 5
    assert paged == full
    assert len(set(paged)) == 5


def test_q_matches_hostname_or_description(client, db_session):
    h1, _ = create_host(client, hostname="web-01")
    h2, _ = create_host(client, hostname="db-01")
    create_host(client, hostname="cache-01")
    db_session.execute(
        update(Host).where(Host.id == uuid.UUID(h2)).values(description="primary WEB database")
    )
    db_session.commit()

    got = {h["hostname"] for h in client.get("/api/v1/hosts", params={"q": "web"}).json()}
    assert got == {"web-01", "db-01"}  # db-01 via its description, case-insensitive


def test_status_filter(client):
    _, ta = create_host(client, hostname="a")
    _, tb = create_host(client, hostname="b")
    _, tc = create_host(client, hostname="c")
    _report(client, ta, "a", packages=[pkg("openssl", candidate="3.1", security=True)])
    _report(client, tb, "b", packages=[pkg("vim", candidate="9.1")])
    _report(client, tc, "c", packages=[pkg("bash")])

    def names(status):
        return sorted(h["hostname"] for h in client.get("/api/v1/hosts", params={"status": status}).json())

    assert names("security") == ["a"]
    assert names("updates") == ["a", "b"]  # security counts as an update too
    assert names("uptodate") == ["c"]
    assert names("all") == ["a", "b", "c"]


def test_include_inactive(client, db_session):
    ha, _ = create_host(client, hostname="a")
    create_host(client, hostname="b")
    db_session.execute(update(Host).where(Host.id == uuid.UUID(ha)).values(is_active=False))
    db_session.commit()

    # default: retired hosts are still listed (the caller filters)
    assert sorted(h["hostname"] for h in client.get("/api/v1/hosts").json()) == ["a", "b"]
    assert [
        h["hostname"]
        for h in client.get("/api/v1/hosts", params={"include_inactive": False}).json()
    ] == ["b"]


def test_freshness_silent_keeps_only_hosts_older_than_5_minutes(client, db_session):
    hf, tf = create_host(client, hostname="fresh")
    ho, to = create_host(client, hostname="overdue")
    create_host(client, hostname="never")  # never reported -> also "silent"
    _report(client, tf, "fresh", packages=[])
    _report(client, to, "overdue", packages=[])
    db_session.execute(
        update(Host).where(Host.id == uuid.UUID(ho)).values(
            last_seen_at=datetime.now(timezone.utc) - timedelta(minutes=8)
        )
    )
    db_session.commit()

    got = sorted(h["hostname"] for h in client.get("/api/v1/hosts", params={"freshness": "silent"}).json())
    assert got == ["never", "overdue"]


def test_tag_filter_key_and_key_value(client, db_session):
    ha, _ = create_host(client, hostname="a")
    hb, _ = create_host(client, hostname="b")
    create_host(client, hostname="c")
    db_session.execute(update(Host).where(Host.id == uuid.UUID(ha)).values(tags={"env": "lab", "role": "app"}))
    db_session.execute(update(Host).where(Host.id == uuid.UUID(hb)).values(tags={"env": "prod"}))
    db_session.commit()

    def names(tag):
        return sorted(h["hostname"] for h in client.get("/api/v1/hosts", params={"tag": tag}).json())

    assert names("env") == ["a", "b"]          # key present
    assert names("LAB") == ["a"]               # value contains, case-insensitive
    assert names("env=prod") == ["b"]          # exact pair
    assert names("env=PROD") == ["b"]          # exact pair, case-insensitive
    assert names("role") == ["a"]


def test_limit_and_filters_compose(client):
    _, ta = create_host(client, hostname="a")
    _, tb = create_host(client, hostname="b")
    create_host(client, hostname="c")
    _report(client, ta, "a", packages=[pkg("x", candidate="2")])
    _report(client, tb, "b", packages=[pkg("y", candidate="2")])

    page1 = client.get("/api/v1/hosts", params={"status": "updates", "limit": 1}).json()
    assert [h["hostname"] for h in page1] == ["a"]
    page2 = client.get(
        "/api/v1/hosts",
        params={"status": "updates", "limit": 1, "after": "a", "after_id": page1[0]["id"]},
    ).json()
    assert [h["hostname"] for h in page2] == ["b"]
