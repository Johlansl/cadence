"""RBAC v1: role resolution, sealed roles, named-permission dependencies.

The provider is never touched here: sessions are sealed directly (the
crypto path is covered by test_oidc_core.py) except for two callback
tests that stub discovery/exchange/validation at the route layer to
prove the login seals the resolved role.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.deps import require_operate, require_read_private
from app.api.routes import auth as auth_routes
from app.auth import oidc as oidc_core
from app.auth import roles
from app.auth.oidc import (
    STATE_PURPOSE,
    STATE_TTL_SECONDS,
    open_session,
    seal_session,
    seal_token,
)
from app.core.config import Settings, settings
from tests.conftest import ADMIN_KEY

OPERATOR_EMAIL = "op@example.com"


def _mini_app() -> FastAPI:
    app = FastAPI()

    @app.post("/op")
    def guarded_write(_: None = Depends(require_operate)):
        return {"ok": True}

    @app.get("/priv")
    def guarded_read(_: None = Depends(require_read_private)):
        return {"ok": True}

    return app


@pytest.fixture()
def mini_client():
    return TestClient(_mini_app())


@pytest.fixture()
def oidc_shaped(monkeypatch):
    """Sessions honored (full OIDC config present), no provider involved."""
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", "https://auth.example.test/o/cadence/")
    monkeypatch.setattr(settings, "oidc_client_id", "cadence-dashboard")
    monkeypatch.setattr(settings, "oidc_client_secret", "provider-secret")
    monkeypatch.setattr(
        settings, "oidc_redirect_uri", "https://cadence.lan/api/v1/auth/oidc/callback"
    )


def _session_cookie(
    *, actor: str = "reader@example.com", role: str | None = "reader"
) -> str:
    kwargs: dict = {}
    if role is not None:
        kwargs["role"] = role
    return seal_session(
        settings.token_encryption_key,
        sub="user-uuid-9",
        email="reader@example.com",
        name=None,
        ttl_seconds=3600,
        actor=actor,
        **kwargs,
    )


# --- config parsing -----------------------------------------------------------


def test_operator_emails_parse_comma_or_space_separated(monkeypatch):
    monkeypatch.setenv(
        "CADENCE_OIDC_OPERATOR_EMAILS", "  Op@Example.com,other@example.com  third@example.com,,"
    )
    assert Settings().oidc_operator_emails == [
        "op@example.com",
        "other@example.com",
        "third@example.com",
    ]


def test_operator_emails_default_empty(monkeypatch):
    monkeypatch.delenv("CADENCE_OIDC_OPERATOR_EMAILS", raising=False)
    assert Settings().oidc_operator_emails == []


# --- role resolution -----------------------------------------------------------


def test_resolve_role_matches_email_or_actor_case_insensitively():
    operators = ["op@example.com"]
    assert (
        roles.resolve_role(actor="someone", email="OP@Example.COM", operators=operators)
        == roles.OPERATOR
    )
    assert (
        roles.resolve_role(actor="Op@Example.com", email=None, operators=operators)
        == roles.OPERATOR
    )
    assert (
        roles.resolve_role(actor="reader@example.com", email=None, operators=operators)
        == roles.READER
    )


def test_resolve_role_empty_allowlist_is_reader():
    assert (
        roles.resolve_role(actor="op@example.com", email="op@example.com", operators=[])
        == roles.READER
    )


def test_role_from_session_roundtrip_and_fail_closed():
    sealed = seal_session(
        settings.token_encryption_key,
        sub="s",
        email="op@example.com",
        name=None,
        ttl_seconds=3600,
        actor="op@example.com",
        role=roles.OPERATOR,
    )
    assert roles.role_from_session(open_session(settings.token_encryption_key, sealed)) == (
        roles.OPERATOR
    )
    assert roles.role_from_session({"sub": "s", "actor": "a"}) == roles.READER
    assert roles.role_from_session({"sub": "s", "actor": "a", "role": "root"}) == (
        roles.READER
    )
    assert roles.role_from_session(None) == roles.READER


# --- named-permission dependencies ----------------------------------------------


def test_key_bypasses_every_permission(mini_client):
    headers = {"X-Admin-Key": ADMIN_KEY}
    assert mini_client.post("/op", headers=headers).status_code == 200
    assert mini_client.get("/priv", headers=headers).status_code == 200


def test_wrong_key_without_session_is_401(mini_client):
    headers = {"X-Admin-Key": "wrong"}
    assert mini_client.post("/op", headers=headers).status_code == 401
    assert mini_client.get("/priv", headers=headers).status_code == 401


def test_missing_key_header_is_422(mini_client):
    assert mini_client.post("/op").status_code == 422


def test_reader_session_403_on_operate_200_on_read_private(mini_client, oidc_shaped):
    cookie = _session_cookie(role=roles.READER)
    headers = {"X-Admin-Key": ""}
    assert (
        mini_client.post("/op", headers=headers, cookies={"cadence_session": cookie}).status_code
        == 403
    )
    assert (
        mini_client.get("/priv", headers=headers, cookies={"cadence_session": cookie}).status_code
        == 200
    )


def test_operator_session_passes_both(mini_client, oidc_shaped):
    cookie = _session_cookie(actor=OPERATOR_EMAIL, role=roles.OPERATOR)
    headers = {"X-Admin-Key": ""}
    assert (
        mini_client.post("/op", headers=headers, cookies={"cadence_session": cookie}).status_code
        == 200
    )
    assert (
        mini_client.get("/priv", headers=headers, cookies={"cadence_session": cookie}).status_code
        == 200
    )


def test_pre_rbac_session_without_role_reads_as_reader(mini_client, oidc_shaped):
    cookie = _session_cookie(role=None)
    headers = {"X-Admin-Key": ""}
    assert (
        mini_client.post("/op", headers=headers, cookies={"cadence_session": cookie}).status_code
        == 403
    )
    assert (
        mini_client.get("/priv", headers=headers, cookies={"cadence_session": cookie}).status_code
        == 200
    )


def test_session_ignored_when_oidc_disabled(mini_client, oidc_shaped, monkeypatch):
    monkeypatch.setattr(settings, "oidc_enabled", False)
    cookie = _session_cookie(actor=OPERATOR_EMAIL, role=roles.OPERATOR)
    r = mini_client.post(
        "/op", headers={"X-Admin-Key": "wrong"}, cookies={"cadence_session": cookie}
    )
    assert r.status_code == 401


# --- login seals the resolved role ------------------------------------------------


def _stub_callback(monkeypatch, claims: dict):
    monkeypatch.setattr(
        oidc_core,
        "discovery",
        lambda issuer: {
            "authorization_endpoint": "https://auth.example.test/authorize/",
            "token_endpoint": "https://auth.example.test/token/",
            "jwks_uri": "https://auth.example.test/jwks/",
        },
    )
    monkeypatch.setattr(
        oidc_core, "exchange_code", lambda *a, **k: {"id_token": "stubbed"}
    )
    monkeypatch.setattr(auth_routes, "_validate_id_token", lambda token, nonce: claims)


def _login(client, monkeypatch, claims: dict) -> str:
    _stub_callback(monkeypatch, claims)
    state = seal_token(
        settings.token_encryption_key,
        STATE_PURPOSE,
        {"state": "st", "nonce": "n", "next": "/"},
        STATE_TTL_SECONDS,
    )
    r = client.get(
        "/api/v1/auth/oidc/callback",
        params={"code": "auth-code-1", "state": "st"},
        cookies={"oidc_state": state},
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    raw = r.cookies["cadence_session"]
    return raw[1:-1] if len(raw) >= 2 and raw.startswith('"') else raw


def test_callback_seals_operator_for_listed_email(client, oidc_shaped, monkeypatch):
    monkeypatch.setattr(settings, "oidc_operator_emails", ["op@example.com"])
    cookie = _login(
        client,
        monkeypatch,
        {"sub": "u-1", "email": "Op@Example.com", "email_verified": True},
    )
    session = open_session(settings.token_encryption_key, cookie)
    assert session is not None
    assert roles.role_from_session(session) == roles.OPERATOR
    body = client.get(
        "/api/v1/auth/me", cookies={"cadence_session": cookie}
    ).json()
    assert body["role"] == roles.OPERATOR


def test_callback_seals_reader_by_default(client, oidc_shaped, monkeypatch):
    monkeypatch.setattr(settings, "oidc_operator_emails", [])
    cookie = _login(
        client,
        monkeypatch,
        {"sub": "u-2", "email": "reader@example.com", "email_verified": True},
    )
    session = open_session(settings.token_encryption_key, cookie)
    assert session is not None
    assert roles.role_from_session(session) == roles.READER
    body = client.get(
        "/api/v1/auth/me", cookies={"cadence_session": cookie}
    ).json()
    assert body["role"] == roles.READER


def test_me_reports_reader_for_pre_rbac_session(client, oidc_shaped):
    cookie = _session_cookie(role=None)
    body = client.get(
        "/api/v1/auth/me", cookies={"cadence_session": cookie}
    ).json()
    assert body["authenticated"] is True
    assert body["role"] == roles.READER
