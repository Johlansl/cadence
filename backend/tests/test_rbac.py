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


# --- lot 2: route-level guards ---------------------------------------------------
#
# The six admin routers carry no router-level guard anymore; every route
# declares its own named permission. These tests lock that structure:
# the filet fails if a route loses its guard entirely, the mapping test
# fails on a wrong permission level, the live matrix proves the behavior.

from app.api.deps import require_operate as dep_operate  # noqa: E402
from app.api.deps import require_read_private as dep_read_private  # noqa: E402
from app.api.routes import (  # noqa: E402
    admin,
    campaigns,
    enrollments,
    exclusions,
    schedules,
    webhooks,
)
from app.main import app as cadence_app  # noqa: E402

ADMIN_ROUTERS = [
    admin.router,
    campaigns.admin_router,
    enrollments.admin_router,
    enrollments.certificate_admin_router,
    exclusions.admin_router,
    schedules.admin_router,
    webhooks.admin_router,
]

DUMMY_UUID = "00000000-0000-0000-0000-000000000000"

# Router paths keep their {placeholders}; the live matrix concretizes them.
OPERATE_ROUTES = [
    ("POST", "/api/v1/admin/hosts"),
    ("PATCH", "/api/v1/admin/hosts/{host_id}"),
    ("DELETE", "/api/v1/admin/hosts/{host_id}"),
    ("POST", "/api/v1/admin/hosts/{host_id}/jobs"),
    ("DELETE", "/api/v1/admin/hosts/{host_id}/jobs"),
    ("POST", "/api/v1/admin/hosts/{host_id}/tokens"),
    ("DELETE", "/api/v1/admin/hosts/{host_id}/tokens/{token_id}"),
    ("POST", "/api/v1/admin/campaigns"),
    ("POST", "/api/v1/admin/campaigns/{campaign_id}/activate"),
    ("POST", "/api/v1/admin/campaigns/{campaign_id}/pause"),
    ("POST", "/api/v1/admin/campaigns/{campaign_id}/resume"),
    ("POST", "/api/v1/admin/campaigns/{campaign_id}/cancel"),
    ("POST", "/api/v1/admin/enrollments"),
    ("DELETE", "/api/v1/admin/enrollments/{enrollment_id}"),
    ("DELETE", "/api/v1/admin/hosts/{host_id}/certificates/{certificate_id}"),
    ("POST", "/api/v1/admin/hosts/{host_id}/schedules"),
    ("PATCH", "/api/v1/admin/schedules/{schedule_id}"),
    ("DELETE", "/api/v1/admin/schedules/{schedule_id}"),
    ("POST", "/api/v1/admin/package-exclusions"),
    ("DELETE", "/api/v1/admin/package-exclusions/{exclusion_id}"),
    ("POST", "/api/v1/admin/webhooks"),
    ("PATCH", "/api/v1/admin/webhooks/{webhook_id}"),
    ("DELETE", "/api/v1/admin/webhooks/{webhook_id}"),
    ("POST", "/api/v1/admin/webhooks/{webhook_id}/test"),
]

READ_PRIVATE_ROUTES = [
    ("GET", "/api/v1/admin/hosts/{host_id}/tokens"),
    ("GET", "/api/v1/admin/audit"),
    ("GET", "/api/v1/admin/enrollments"),
    ("GET", "/api/v1/admin/hosts/{host_id}/certificates"),
]


def _concrete(path: str) -> str:
    """Fill path placeholders with valid values (dependency runs before
    the handler, but invalid ids could 422 before it)."""
    return (
        path.replace("{token_id}", "1")
        .replace("{certificate_id}", "1")
        .replace("{host_id}", DUMMY_UUID)
        .replace("{campaign_id}", DUMMY_UUID)
        .replace("{enrollment_id}", DUMMY_UUID)
        .replace("{schedule_id}", DUMMY_UUID)
        .replace("{exclusion_id}", DUMMY_UUID)
        .replace("{webhook_id}", DUMMY_UUID)
    )


def _router_route_permissions() -> dict:
    """(method, path) -> permission | None, read off the seven admin
    routers by dependency function identity."""
    found = {}
    for router in ADMIN_ROUTERS:
        for route in router.routes:
            deps = {d.dependency for d in getattr(route, "dependencies", [])}
            if dep_operate in deps:
                perm: str | None = roles.OPERATE
            elif dep_read_private in deps:
                perm = roles.READ_PRIVATE
            else:
                perm = None
            methods = sorted(
                m
                for m in getattr(route, "methods", set())
                if m not in ("HEAD", "OPTIONS")
            )
            for method in methods:
                found[(method, route.path)] = perm
    return found


def _app_admin_routes() -> set:
    """(method, path) for every /api/v1/admin path in the live app schema,
    so a route added on any router (or a new router) cannot slip past."""
    return {
        (method.upper(), path)
        for path, item in cadence_app.openapi()["paths"].items()
        if path.startswith("/api/v1/admin")
        for method in item
        if method.upper() not in ("HEAD", "OPTIONS", "PARAMETERS")
    }


def test_no_admin_route_without_named_permission():
    missing = sorted(
        f"{method} {path}"
        for (method, path), perm in _router_route_permissions().items()
        if perm is None
    )
    assert missing == []


def test_admin_route_permission_mapping_is_exact():
    expected = {route: roles.OPERATE for route in OPERATE_ROUTES}
    expected.update({route: roles.READ_PRIVATE for route in READ_PRIVATE_ROUTES})
    assert _router_route_permissions() == expected
    assert _app_admin_routes() == set(expected)


def _op_headers() -> dict:
    cookie = _session_cookie(actor=OPERATOR_EMAIL, role=roles.OPERATOR)
    return {"X-Admin-Key": "", "Cookie": f"cadence_session={cookie}"}


def _reader_headers() -> dict:
    cookie = _session_cookie(role=roles.READER)
    return {"X-Admin-Key": "", "Cookie": f"cadence_session={cookie}"}


def test_reader_forbidden_on_every_operate_route(client, oidc_shaped):
    headers = _reader_headers()
    for method, path in OPERATE_ROUTES:
        url = _concrete(path)
        r = client.request(method, url, headers=headers, json={})
        assert r.status_code == 403, (method, url, r.status_code, r.text)


def test_operator_allowed_on_protected_reads(client, db_session, oidc_shaped):
    host_id = _create_host_op(client)
    for _method, path in READ_PRIVATE_ROUTES:
        url = _concrete(path).replace(DUMMY_UUID, host_id)
        assert client.get(url, headers=_reader_headers()).status_code == 200, path
    assert (
        client.get("/api/v1/admin/audit", headers=_op_headers()).status_code == 200
    )


def _create_host_op(client) -> str:
    r = client.post(
        "/api/v1/admin/hosts", headers=_op_headers(), json={"hostname": "rbac-matrix"}
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_operator_writes_hosts_jobs_tokens(client, oidc_shaped):
    host_id = _create_host_op(client)
    headers = _op_headers()
    assert (
        client.patch(
            f"/api/v1/admin/hosts/{host_id}",
            headers=headers,
            json={"tags": {"env": "rbac"}},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/admin/hosts/{host_id}/jobs", headers=headers, json={}
        ).status_code
        == 201
    )
    assert (
        client.post(
            f"/api/v1/admin/hosts/{host_id}/tokens", headers=headers, json={}
        ).status_code
        == 201
    )


def test_operator_writes_schedules_exclusions(client, oidc_shaped):
    host_id = _create_host_op(client)
    headers = _op_headers()
    r = client.post(
        f"/api/v1/admin/hosts/{host_id}/schedules",
        headers=headers,
        json={"kind": "weekly", "weekday": 6, "hour": 3, "minute": 0, "timezone": "UTC"},
    )
    assert r.status_code == 201, r.text
    sched_id = r.json()["id"]
    assert (
        client.patch(
            f"/api/v1/admin/schedules/{sched_id}", headers=headers, json={"hour": 4}
        ).status_code
        == 200
    )
    r = client.post(
        "/api/v1/admin/package-exclusions",
        headers=headers,
        json={"scope": "global", "pattern": "linux-image*"},
    )
    assert r.status_code == 201, r.text
    assert (
        client.delete(
            f"/api/v1/admin/package-exclusions/{r.json()['id']}", headers=headers
        ).status_code
        == 204
    )
    assert (
        client.delete(f"/api/v1/admin/schedules/{sched_id}", headers=headers).status_code
        == 204
    )


def test_operator_writes_webhooks_campaigns_enrollments(
    client, oidc_shaped, tmp_path, monkeypatch
):
    from app.pki.client_ca import ensure_client_ca

    material = ensure_client_ca(tmp_path / "server-ca")
    monkeypatch.setattr(
        "app.api.routes.enrollments.settings.server_ca_file",
        str(material.root_certificate),
    )
    host_id = _create_host_op(client)
    headers = _op_headers()
    r = client.post(
        "/api/v1/admin/webhooks",
        headers=headers,
        json={
            "url": "https://hooks.example.test/api/webhooks/42/aaaaaaaaaaaaaaaa",
            "event_types": ["job.succeeded"],
        },
    )
    assert r.status_code == 201, r.text
    hook_id = r.json()["id"]
    assert (
        client.patch(
            f"/api/v1/admin/webhooks/{hook_id}",
            headers=headers,
            json={"description": "rbac"},
        ).status_code
        == 200
    )
    assert (
        client.post(f"/api/v1/admin/webhooks/{hook_id}/test", headers=headers).status_code
        == 202
    )
    r = client.post(
        "/api/v1/admin/campaigns",
        headers=headers,
        json={
            "name": "rbac tuesday",
            "host_ids": [host_id],
            "stages": [1],
            "max_concurrency": 1,
            "max_failures": 1,
        },
    )
    assert r.status_code == 201, r.text
    assert (
        client.post(
            f"/api/v1/admin/campaigns/{r.json()['id']}/activate", headers=headers
        ).status_code
        == 200
    )
    r = client.post(
        "/api/v1/admin/enrollments",
        headers=headers,
        json={"expected_hostname": "rbac-new"},
    )
    assert r.status_code == 201, r.text
    assert (
        client.delete(f"/api/v1/admin/webhooks/{hook_id}", headers=headers).status_code
        == 204
    )
