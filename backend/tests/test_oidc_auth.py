"""Tests for the OIDC auth endpoints and dual admin auth (roadmap item 10).

The provider is fully stubbed (real JWTs minted with a self-signed RSA key,
never any network): discovery, code exchange and JWKS are monkeypatched at
the route layer. The one live-provider proof happens against the reference
Authentik deployment, not here.
"""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.auth import oidc as oidc_mod
from app.auth.oidc import STATE_PURPOSE, open_token
from app.core.config import settings

ISSUER = "https://auth.example.com/application/o/cadence/"
CLIENT_ID = "cadence-dashboard"
REDIRECT_URI = "https://cadence.lan/api/v1/auth/oidc/callback"
AUTH_ENDPOINT = ISSUER + "authorize/"
TOKEN_ENDPOINT = ISSUER + "token/"
JWKS_URI = ISSUER + "jwks/"
RSA_KID = "test-rsa-1"
NOW = 1780000000


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def jwks(rsa_key):
    numbers = rsa_key.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": RSA_KID,
                "use": "sig",
                "alg": "RS256",
                "n": _b64url(numbers.n.to_bytes(256, "big")),
                "e": _b64url(numbers.e.to_bytes(3, "big")),
            }
        ]
    }


def _mint(key, claims: dict) -> str:
    header = {"typ": "JWT", "alg": "RS256", "kid": RSA_KID}
    signing_input = (
        f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"
    )
    sig = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(sig)}"


def _claims(**over) -> dict:
    base = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "exp": NOW + 300,
        "iat": NOW - 10,
        "nonce": "nonce-from-state",
        "sub": "user-uuid-1",
        "email": "johlan@example.com",
        "email_verified": True,
        "preferred_username": "johlan",
    }
    base.update(over)
    return base


@pytest.fixture()
def oidc_on(monkeypatch):
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", ISSUER)
    monkeypatch.setattr(settings, "oidc_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "oidc_client_secret", "provider-secret")
    monkeypatch.setattr(settings, "oidc_redirect_uri", REDIRECT_URI)
    monkeypatch.setattr(settings, "oidc_cookie_secure", False)


@pytest.fixture()
def stub_provider(monkeypatch, rsa_key, jwks):
    """Stub discovery/exchange/JWKS. The minted ID token's nonce follows
    `ctx["nonce"]`, which each test sets from its login state cookie; time
    is frozen at NOW so exp/iat check out."""
    import app.auth.oidc as core

    ctx = {"nonce": None}
    monkeypatch.setattr(
        oidc_mod,
        "discovery",
        lambda issuer: {
            "authorization_endpoint": AUTH_ENDPOINT,
            "token_endpoint": TOKEN_ENDPOINT,
            "jwks_uri": JWKS_URI,
        },
    )
    monkeypatch.setattr(oidc_mod, "get_jwks", lambda uri: jwks)
    monkeypatch.setattr(oidc_mod, "refresh_jwks", lambda uri: jwks)

    def fake_exchange(token_endpoint, **kw):
        assert token_endpoint == TOKEN_ENDPOINT
        assert kw["redirect_uri"] == REDIRECT_URI
        return {
            "id_token": _mint(rsa_key, _claims(nonce=ctx["nonce"])),
            "token_type": "Bearer",
        }

    monkeypatch.setattr(oidc_mod, "exchange_code", fake_exchange)
    monkeypatch.setattr(core.time, "time", lambda: NOW)
    return ctx


def _unquote_browser(value: str) -> str:
    """Browsers unquote DQUOTE-wrapped cookie values before sending them
    back (Starlette unquotes on parse); httpx exposes the raw bytes, so the
    test does the browser's job here. Starlette quotes values ending in the
    Fernet `=` padding depending on the random tail."""
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def _login_state(client, ctx, next="/hosts") -> tuple[str, dict]:
    r = client.get(f"/api/v1/auth/oidc/login?next={next}", follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers["location"].startswith(AUTH_ENDPOINT)
    assert "state=" in r.headers["location"] and "nonce=" in r.headers["location"]
    assert "oidc_state" in r.cookies
    cookie = _unquote_browser(r.cookies["oidc_state"])
    data = open_token(settings.token_encryption_key, STATE_PURPOSE, cookie)
    assert data is not None
    ctx["nonce"] = data["nonce"]
    return cookie, data, r.headers["location"]


def _callback(client, state_cookie: str, **params) -> object:
    return client.get(
        "/api/v1/auth/oidc/callback",
        params={"code": "auth-code-1", **params},
        cookies={"oidc_state": state_cookie},
        follow_redirects=False,
    )


def test_login_redirect_and_state_cookie(client, oidc_on, stub_provider):
    ctx = stub_provider
    _, data, location = _login_state(client, ctx)
    assert CLIENT_ID in location
    assert data["next"] == "/hosts"


def test_login_rejects_open_redirect(client, oidc_on, stub_provider):
    ctx = stub_provider
    _, data, _ = _login_state(client, ctx, next="https://evil.example.com/")
    assert data["next"] == "/"


def test_callback_sets_session_and_redirects(client, oidc_on, stub_provider):
    ctx = stub_provider
    state_cookie, data, _ = _login_state(client, ctx)
    r = _callback(client, state_cookie, state=data["state"])
    assert r.status_code == 302, r.text
    assert r.headers["location"] == "/hosts"
    assert "cadence_session" in r.cookies


def test_admin_write_with_session_records_oidc_actor(
    client, db_session, oidc_on, stub_provider
):
    ctx = stub_provider
    state_cookie, data, _ = _login_state(client, ctx)
    r = _callback(client, state_cookie, state=data["state"])
    assert r.status_code == 302, r.text

    # Session auth with an empty key header and a spoofed X-Actor: the audit
    # row must carry the verified subject, never the header.
    r = client.post(
        "/api/v1/admin/hosts",
        json={"hostname": "sso-host"},
        headers={"X-Admin-Key": "", "X-Actor": "attacker"},
    )
    assert r.status_code == 201, r.text
    rows = client.get("/api/v1/admin/audit", headers={"X-Admin-Key": ""}).json()
    created = [a for a in rows if a["action"] == "host.create"]
    assert created and created[0]["actor"] == "johlan@example.com"


def test_callback_rejects_bad_state_and_provider_error(
    client, oidc_on, stub_provider
):
    ctx = stub_provider
    state_cookie, _, _ = _login_state(client, ctx)
    assert _callback(client, state_cookie, state="wrong").status_code == 400
    assert _callback(client, "garbage", state="x").status_code == 400
    r = client.get(
        "/api/v1/auth/oidc/callback",
        params={"error": "access_denied"},
        cookies={"oidc_state": state_cookie},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_unknown_kid_triggers_one_jwks_refresh(
    client, oidc_on, stub_provider, monkeypatch, rsa_key, jwks
):
    ctx = stub_provider
    calls = {"get": 0, "refresh": 0}

    def get_v1(uri):
        calls["get"] += 1
        return {"keys": []}

    def refresh_v1(uri):
        calls["refresh"] += 1
        return jwks

    monkeypatch.setattr(oidc_mod, "get_jwks", get_v1)
    monkeypatch.setattr(oidc_mod, "refresh_jwks", refresh_v1)
    state_cookie, data, _ = _login_state(client, ctx)
    r = _callback(client, state_cookie, state=data["state"])
    assert r.status_code == 302, r.text
    assert calls == {"get": 1, "refresh": 1}


def test_me_and_logout(client, oidc_on, stub_provider):
    ctx = stub_provider
    assert client.get("/api/v1/auth/me").json() == {"authenticated": False}
    state_cookie, data, _ = _login_state(client, ctx)
    _callback(client, state_cookie, state=data["state"])
    body = client.get("/api/v1/auth/me").json()
    assert body == {
        "authenticated": True,
        "actor": "johlan@example.com",
        "sub": "user-uuid-1",
    }
    assert client.post("/api/v1/auth/logout").json() == {"ok": True}
    assert client.get("/api/v1/auth/me").json() == {"authenticated": False}


def test_endpoints_404_when_disabled(client):
    assert client.get("/api/v1/auth/oidc/login", follow_redirects=False).status_code == 404
    assert client.get("/api/v1/auth/me").status_code == 404
    assert client.post("/api/v1/auth/logout").status_code == 404
