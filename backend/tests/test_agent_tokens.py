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

from app.core.crypto import decrypt_token_secret
from app.models.models import AgentToken, AuditLog, Host
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
    assert tok.revoked_at is None
    # Default expiry, not NULL (docs/decisions.md "Authentication").
    assert tok.expires_at is not None
    assert tok.expires_at > datetime.now(timezone.utc) + timedelta(days=364)
    assert tok.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert decrypt_token_secret(tok.secret_encrypted) == token

    r = client.post("/api/v1/reports", headers=bearer(token), json=report_payload())
    assert r.status_code == 200


def _last_used(db, host_id):
    db.expire_all()
    return db.execute(
        select(AgentToken.last_used_at).where(AgentToken.host_id == host_id)
    ).scalar_one()


def test_auth_updates_last_used_at(client, db_session):
    host_id, token = create_host(client)
    assert _last_used(db_session, host_id) is None

    client.post("/api/v1/agent/next-job", headers=bearer(token))

    assert _last_used(db_session, host_id) is not None


def test_last_used_at_is_not_rewritten_every_request(client, db_session):
    host_id, token = create_host(client)
    client.post("/api/v1/agent/next-job", headers=bearer(token))
    first = _last_used(db_session, host_id)
    assert first is not None

    # A second call within the resolution window must not touch the row.
    client.post("/api/v1/agent/next-job", headers=bearer(token))
    assert _last_used(db_session, host_id) == first


def test_last_used_at_advances_once_the_interval_passes(client, db_session, monkeypatch):
    monkeypatch.setattr("app.api.deps._LAST_USED_MIN_INTERVAL", timedelta(0))
    host_id, token = create_host(client)
    client.post("/api/v1/agent/next-job", headers=bearer(token))
    first = _last_used(db_session, host_id)

    client.post("/api/v1/agent/next-job", headers=bearer(token))
    assert _last_used(db_session, host_id) > first


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


# --- issue / list / revoke endpoints -------------------------------------


def test_issue_list_revoke_flow(client, db_session):
    host_id, initial = create_host(client)

    r = client.post(
        f"/api/v1/admin/hosts/{host_id}/tokens",
        headers=ADMIN_HEADERS,
        json={"label": "rotated"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    new_id, secret = body["id"], body["token"]
    assert body["label"] == "rotated" and len(secret) >= 32

    # the new token authenticates
    assert client.post(
        "/api/v1/reports", headers=bearer(secret), json=report_payload()
    ).status_code == 200

    lst = client.get(
        f"/api/v1/admin/hosts/{host_id}/tokens", headers=ADMIN_HEADERS
    ).json()
    assert len(lst) == 2
    assert lst[0]["id"] == new_id  # newest active first
    assert {t["state"] for t in lst} == {"active"}
    fields = {"id", "label", "created_at", "last_used_at", "expires_at", "revoked_at", "state"}
    for t in lst:
        assert fields <= t.keys()
        assert "token_hash" not in t and "token" not in t

    # revoke the new one
    assert client.delete(
        f"/api/v1/admin/hosts/{host_id}/tokens/{new_id}", headers=ADMIN_HEADERS
    ).status_code == 204
    assert client.post(
        "/api/v1/reports", headers=bearer(secret), json=report_payload()
    ).status_code == 401
    assert client.post(
        "/api/v1/reports", headers=bearer(initial), json=report_payload()
    ).status_code == 200  # the initial token is untouched

    lst = client.get(
        f"/api/v1/admin/hosts/{host_id}/tokens", headers=ADMIN_HEADERS
    ).json()
    by_id = {t["id"]: t for t in lst}
    assert by_id[new_id]["state"] == "revoked" and by_id[new_id]["revoked_at"] is not None
    assert lst[-1]["id"] == new_id  # revoked sorts after active

    # second revoke is a no-op 204
    assert client.delete(
        f"/api/v1/admin/hosts/{host_id}/tokens/{new_id}", headers=ADMIN_HEADERS
    ).status_code == 204


def test_issue_with_past_expiry_is_rejected(client):
    host_id, _ = create_host(client)
    r = client.post(
        f"/api/v1/admin/hosts/{host_id}/tokens",
        headers=ADMIN_HEADERS,
        json={"expires_at": "2000-01-01T00:00:00Z"},
    )
    assert r.status_code == 422


def test_expired_token_shows_expired_state(client, db_session):
    host_id, _ = create_host(client)
    tid = client.post(
        f"/api/v1/admin/hosts/{host_id}/tokens", headers=ADMIN_HEADERS, json={}
    ).json()["id"]
    tok = db_session.get(AgentToken, tid)
    tok.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()

    lst = client.get(
        f"/api/v1/admin/hosts/{host_id}/tokens", headers=ADMIN_HEADERS
    ).json()
    assert {t["id"]: t["state"] for t in lst}[tid] == "expired"


def test_token_endpoints_unknown_host_404(client):
    import uuid

    missing = uuid.uuid4()
    assert client.get(
        f"/api/v1/admin/hosts/{missing}/tokens", headers=ADMIN_HEADERS
    ).status_code == 404
    assert client.post(
        f"/api/v1/admin/hosts/{missing}/tokens", headers=ADMIN_HEADERS, json={}
    ).status_code == 404

    host_id, _ = create_host(client)
    assert client.delete(
        f"/api/v1/admin/hosts/{host_id}/tokens/999999", headers=ADMIN_HEADERS
    ).status_code == 404


def test_revoke_rejects_a_token_from_another_host(client, db_session):
    host_a, _ = create_host(client, "vm-a")
    host_b, _ = create_host(client, "vm-b")
    tid = db_session.execute(
        select(AgentToken.id).where(AgentToken.host_id == host_b)
    ).scalar_one()

    assert client.delete(
        f"/api/v1/admin/hosts/{host_a}/tokens/{tid}", headers=ADMIN_HEADERS
    ).status_code == 404


def test_token_issue_and_revoke_are_audited(client, db_session):
    host_id, _ = create_host(client)
    tid = client.post(
        f"/api/v1/admin/hosts/{host_id}/tokens", headers=ADMIN_HEADERS, json={"label": "ci"}
    ).json()["id"]
    client.delete(f"/api/v1/admin/hosts/{host_id}/tokens/{tid}", headers=ADMIN_HEADERS)

    actions = db_session.execute(
        select(AuditLog.action)
        .where(AuditLog.target_type == "token", AuditLog.target_id == str(tid))
        .order_by(AuditLog.id)
    ).scalars().all()
    assert actions == ["token.issue", "token.revoke"]
