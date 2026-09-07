"""The signed-request auth path (docs/decisions.md "Authentication"): the raw
token never crosses the wire, an HMAC-SHA256 over timestamp+method+path+body
does instead, keyed with the real secret recovered via app.core.crypto. The
legacy bearer path (test_agent_tokens.py) is untouched and still works
alongside it during the transition."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.crypto import encrypt_token_secret
from tests.conftest import ADMIN_HEADERS, create_host, report_payload

from .test_agent_tokens import _add_token


def _sign(secret: str, method: str, path: str, body: bytes, *, ts: int | None = None) -> dict:
    timestamp = str(ts if ts is not None else int(time.time()))
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = f"{timestamp}\n{method}\n{path}\n{body_hash}"
    sig = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {
        "X-Cadence-Token-Hash": hashlib.sha256(secret.encode()).hexdigest(),
        "X-Cadence-Timestamp": timestamp,
        "X-Cadence-Signature": sig,
    }


def _post(client, path: str, secret: str, payload: dict, **sign_kwargs):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", **_sign(secret, "POST", path, body, **sign_kwargs)}
    return client.post(path, content=body, headers=headers)


def test_valid_signature_is_accepted(client):
    host_id, token = create_host(client)
    r = _post(client, "/api/v1/reports", token, report_payload())
    assert r.status_code == 200, r.text


def test_wrong_secret_is_rejected(client):
    host_id, token = create_host(client)
    body = json.dumps(report_payload()).encode()
    headers = {
        "Content-Type": "application/json",
        **_sign("not-the-real-secret", "POST", "/api/v1/reports", body),
    }
    # The lookup hash must still match a real token, or this is just "unknown
    # token" -- sign with the wrong secret but claim the right token.
    headers["X-Cadence-Token-Hash"] = hashlib.sha256(token.encode()).hexdigest()
    r = client.post("/api/v1/reports", content=body, headers=headers)
    assert r.status_code == 401


def test_tampered_body_is_rejected(client):
    host_id, token = create_host(client)
    signed_body = json.dumps(report_payload()).encode()
    headers = {
        "Content-Type": "application/json",
        **_sign(token, "POST", "/api/v1/reports", signed_body),
    }
    # Send different bytes than what was signed.
    r = client.post(
        "/api/v1/reports", content=json.dumps(report_payload(hostname="evil")).encode(),
        headers=headers,
    )
    assert r.status_code == 401


def test_tampered_path_is_rejected(client):
    host_id, token = create_host(client)
    body = json.dumps({}).encode()
    # Signed for next-job, sent to reports.
    headers = {
        "Content-Type": "application/json",
        **_sign(token, "POST", "/api/v1/agent/next-job", body),
    }
    r = client.post("/api/v1/reports", content=body, headers=headers)
    assert r.status_code == 401


def test_tampered_method_is_rejected(client):
    host_id, token = create_host(client)
    body = json.dumps({}).encode()
    headers = {
        "Content-Type": "application/json",
        **_sign(token, "GET", "/api/v1/agent/next-job", body),
    }
    r = client.post("/api/v1/agent/next-job", content=body, headers=headers)
    assert r.status_code == 401


def test_stale_timestamp_is_rejected(client):
    host_id, token = create_host(client)
    old = int(time.time()) - settings.signature_window_seconds - 30
    r = _post(client, "/api/v1/reports", token, report_payload(), ts=old)
    assert r.status_code == 401


def test_future_timestamp_is_rejected(client):
    host_id, token = create_host(client)
    ahead = int(time.time()) + settings.signature_window_seconds + 30
    r = _post(client, "/api/v1/reports", token, report_payload(), ts=ahead)
    assert r.status_code == 401


def test_timestamp_within_window_is_accepted(client):
    host_id, token = create_host(client)
    close = int(time.time()) - settings.signature_window_seconds + 10
    r = _post(client, "/api/v1/reports", token, report_payload(), ts=close)
    assert r.status_code == 200, r.text


def test_partial_signed_headers_are_rejected_outright(client, db_session):
    host_id, token = create_host(client)
    body = json.dumps(report_payload()).encode()
    full = _sign(token, "POST", "/api/v1/reports", body)
    for missing in ("X-Cadence-Token-Hash", "X-Cadence-Timestamp", "X-Cadence-Signature"):
        headers = {"Content-Type": "application/json", **full}
        del headers[missing]
        r = client.post("/api/v1/reports", content=body, headers=headers)
        assert r.status_code == 401, f"missing {missing}: {r.text}"


def test_revoked_token_rejected_via_signed_path(client):
    host_id, token = create_host(client)
    tok_id = client.get(
        f"/api/v1/admin/hosts/{host_id}/tokens", headers=ADMIN_HEADERS
    ).json()[0]["id"]
    assert client.delete(
        f"/api/v1/admin/hosts/{host_id}/tokens/{tok_id}", headers=ADMIN_HEADERS
    ).status_code == 204

    r = _post(client, "/api/v1/reports", token, report_payload())
    assert r.status_code == 401


def test_expired_token_rejected_via_signed_path(client, db_session):
    host_id, _ = create_host(client)
    secret = "expired-secret-for-signing"
    _add_token(
        db_session, host_id, secret=secret,
        secret_encrypted=encrypt_token_secret(secret),
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    r = _post(client, "/api/v1/reports", secret, report_payload())
    assert r.status_code == 401


def test_pre_migration_token_cannot_sign_but_still_works_as_bearer(client, db_session):
    """A token with no secret_encrypted (issued before this scheme existed,
    or never rotated onto it) can never verify a signature -- it can only
    ever authenticate via the legacy bearer path."""
    host_id, _ = create_host(client)
    secret = "legacy-secret-no-encrypted-copy"
    _add_token(db_session, host_id, secret=secret)  # no secret_encrypted

    r = _post(client, "/api/v1/reports", secret, report_payload())
    assert r.status_code == 401

    r = client.post(
        "/api/v1/reports",
        headers={"Authorization": f"Bearer {secret}"},
        json=report_payload(),
    )
    assert r.status_code == 200, r.text


def test_unknown_token_hash_is_rejected(client):
    body = json.dumps(report_payload()).encode()
    headers = {
        "Content-Type": "application/json",
        **_sign("some-secret-nobody-issued", "POST", "/api/v1/reports", body),
    }
    r = client.post("/api/v1/reports", content=body, headers=headers)
    assert r.status_code == 401
