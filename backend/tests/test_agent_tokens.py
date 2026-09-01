"""agent_tokens (migration 0008) and the get_current_host auth chain.

The auth chain is a single ordered path:
    token -> agent_tokens row -> not revoked / not expired -> host -> is_active
Each step can only reject; `hosts.is_active` stays decisive regardless of
token state.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.models import AgentToken, Host
from tests.conftest import ADMIN_HEADERS, bearer, create_host, report_payload


def _add_token(db, host_id, *, secret, **cols) -> None:
    db.add(
        AgentToken(
            host_id=host_id,
            token_hash=hashlib.sha256(secret.encode()).hexdigest(),
            **cols,
        )
    )
    db.commit()


def test_create_host_makes_one_active_token(client, db_session):
    host_id, token = create_host(client)

    rows = db_session.execute(
        select(AgentToken).where(AgentToken.host_id == host_id)
    ).scalars().all()
    assert len(rows) == 1
    tok = rows[0]
    assert tok.label == "initial"
    assert tok.revoked_at is None and tok.expires_at is None
    assert tok.token_hash == hashlib.sha256(token.encode()).hexdigest()

    r = client.post("/api/v1/reports", headers=bearer(token), json=report_payload())
    assert r.status_code == 200


def test_auth_updates_last_used_at(client, db_session):
    host_id, token = create_host(client)
    assert db_session.execute(
        select(AgentToken.last_used_at).where(AgentToken.host_id == host_id)
    ).scalar_one() is None

    client.post("/api/v1/agent/next-job", headers=bearer(token))

    db_session.expire_all()
    assert db_session.execute(
        select(AgentToken.last_used_at).where(AgentToken.host_id == host_id)
    ).scalar_one() is not None


def test_revoked_token_is_rejected(client, db_session):
    host_id, token = create_host(client)
    tok = db_session.execute(
        select(AgentToken).where(AgentToken.host_id == host_id)
    ).scalar_one()
    tok.revoked_at = datetime.now(timezone.utc)
    db_session.commit()

    r = client.post("/api/v1/reports", headers=bearer(token), json=report_payload())
    assert r.status_code == 401


def test_expired_token_is_rejected(client, db_session):
    host_id, token = create_host(client)
    tok = db_session.execute(
        select(AgentToken).where(AgentToken.host_id == host_id)
    ).scalar_one()
    tok.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()

    r = client.post("/api/v1/reports", headers=bearer(token), json=report_payload())
    assert r.status_code == 401

    # a not-yet-reached expiry still works
    tok.expires_at = datetime.now(timezone.utc) + timedelta(days=1)
    db_session.commit()
    r = client.post("/api/v1/reports", headers=bearer(token), json=report_payload())
    assert r.status_code == 200


def test_multiple_active_tokens_all_authenticate(client, db_session):
    host_id, first = create_host(client)
    _add_token(db_session, host_id, secret="second-token-secret", label="rotated")

    for secret in (first, "second-token-secret"):
        r = client.post(
            "/api/v1/reports", headers=bearer(secret), json=report_payload()
        )
        assert r.status_code == 200, secret


def test_inactive_host_rejects_a_valid_token(client, db_session):
    """The host gate is independent of token state."""
    host_id, token = create_host(client)
    client.patch(
        f"/api/v1/admin/hosts/{host_id}", headers=ADMIN_HEADERS, json={"is_active": False}
    )

    r = client.post("/api/v1/reports", headers=bearer(token), json=report_payload())
    assert r.status_code == 401

    client.patch(
        f"/api/v1/admin/hosts/{host_id}", headers=ADMIN_HEADERS, json={"is_active": True}
    )
    r = client.post("/api/v1/reports", headers=bearer(token), json=report_payload())
    assert r.status_code == 200


def test_deleting_a_host_cascades_to_its_tokens(client, db_session):
    host_id, _ = create_host(client)
    _add_token(db_session, host_id, secret="another", label="x")
    assert db_session.execute(
        select(AgentToken).where(AgentToken.host_id == host_id)
    ).scalars().all()

    client.delete(f"/api/v1/admin/hosts/{host_id}", headers=ADMIN_HEADERS)

    db_session.expire_all()
    assert db_session.execute(
        select(AgentToken).where(AgentToken.host_id == host_id)
    ).scalars().all() == []
    assert db_session.get(Host, host_id) is None
