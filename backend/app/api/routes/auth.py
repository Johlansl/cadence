"""Admin SSO entry points (roadmap item 10).

Authorization Code Flow against the configured OIDC provider, coexisting
with the shared X-Admin-Key (which keeps working exactly as before: see
`app.api.deps.require_admin_key`). Every endpoint here 404s unless OIDC is
enabled AND fully configured, so default deployments behave bit-for-bit as
before. Login and callback are unauthenticated by nature; they sit under
the dashboard rate-limit surface like any keyless request.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth import oidc
from app.auth.oidc import (
    STATE_PURPOSE,
    STATE_TTL_SECONDS,
    OidcError,
    UnknownKidError,
    actor_from_claims,
)
from app.core.config import settings

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

SESSION_COOKIE = "cadence_session"
STATE_COOKIE = "oidc_state"


def _configured() -> bool:
    return bool(
        settings.oidc_enabled
        and settings.oidc_issuer
        and settings.oidc_client_id
        and settings.oidc_client_secret
        and settings.oidc_redirect_uri
    )


def _require_configured() -> None:
    if not _configured():
        raise HTTPException(status_code=404, detail="not found")


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


@router.get("/oidc/login")
def oidc_login(request: Request, next: str | None = Query(default="/")):
    """Start the login: seal state+nonce in a short cookie and redirect the
    browser to the provider."""
    _require_configured()
    try:
        metadata = oidc.discovery(settings.oidc_issuer)
    except OidcError as exc:
        raise HTTPException(status_code=502, detail="identity provider unreachable") from exc
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    state_token = oidc.seal_token(
        settings.token_encryption_key,
        STATE_PURPOSE,
        {"state": state, "nonce": nonce, "next": _safe_next(next)},
        STATE_TTL_SECONDS,
    )
    target = oidc.build_authorize_url(
        metadata["authorization_endpoint"],
        client_id=settings.oidc_client_id,
        redirect_uri=settings.oidc_redirect_uri,
        scope=settings.oidc_scopes,
        state=state,
        nonce=nonce,
    )
    response = RedirectResponse(target, status_code=302)
    response.set_cookie(
        STATE_COOKIE,
        state_token,
        httponly=True,
        secure=settings.oidc_cookie_secure,
        samesite="lax",
        path="/api/v1/auth",
        max_age=STATE_TTL_SECONDS,
    )
    return response


def _validate_id_token(id_token: str, nonce: str) -> dict:
    """Validate with the cached JWKS, allowing exactly one refresh for an
    unknown kid (key rotation); every other failure is final."""
    metadata = oidc.discovery(settings.oidc_issuer)
    jwks = oidc.get_jwks(metadata["jwks_uri"])
    try:
        return oidc.validate_id_token(
            id_token,
            jwks=jwks,
            issuer=settings.oidc_issuer,
            client_id=settings.oidc_client_id,
            nonce=nonce,
            skew_seconds=settings.oidc_clock_skew_seconds,
        )
    except UnknownKidError:
        jwks = oidc.refresh_jwks(metadata["jwks_uri"])
        return oidc.validate_id_token(
            id_token,
            jwks=jwks,
            issuer=settings.oidc_issuer,
            client_id=settings.oidc_client_id,
            nonce=nonce,
            skew_seconds=settings.oidc_clock_skew_seconds,
        )


@router.get("/oidc/callback")
def oidc_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
):
    """Provider redirect target: verify state, exchange the code, validate
    the ID token, set the session cookie, return to the dashboard."""
    _require_configured()
    if error:
        raise HTTPException(status_code=400, detail="identity provider refused")
    state_data = oidc.open_token(
        settings.token_encryption_key, STATE_PURPOSE, request.cookies.get(STATE_COOKIE, "")
    )
    if (
        state_data is None
        or not code
        or not state
        or not secrets.compare_digest(str(state_data.get("state", "")), state)
    ):
        raise HTTPException(status_code=400, detail="invalid login state")
    try:
        metadata = oidc.discovery(settings.oidc_issuer)
        tokens = oidc.exchange_code(
            metadata["token_endpoint"],
            code=code,
            redirect_uri=settings.oidc_redirect_uri,
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
        )
        claims = _validate_id_token(tokens["id_token"], str(state_data.get("nonce", "")))
    except OidcError as exc:
        raise HTTPException(status_code=401, detail="sign-in failed") from exc
    actor = actor_from_claims(claims)
    session = oidc.seal_session(
        settings.token_encryption_key,
        sub=str(claims.get("sub", "")),
        email=claims.get("email") if isinstance(claims.get("email"), str) else None,
        name=claims.get("preferred_username")
        if isinstance(claims.get("preferred_username"), str)
        else None,
        ttl_seconds=settings.oidc_session_ttl_seconds,
        actor=actor,
    )
    response = RedirectResponse(_safe_next(state_data.get("next")), status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        session,
        httponly=True,
        secure=settings.oidc_cookie_secure,
        samesite="lax",
        path="/",
        max_age=settings.oidc_session_ttl_seconds,
    )
    response.delete_cookie(STATE_COOKIE, path="/api/v1/auth")
    return response


@router.post("/logout")
def logout():
    """Local logout only (v1): clears the session cookie. The provider SSO
    session, if any, is untouched -- documented in docs/oidc.md."""
    _require_configured()
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/me")
def me(request: Request):
    """Session status for the dashboard (no secret): who is signed in, if
    anyone. Anonymous callers get authenticated=false, never a 401, so the
    UI can render both states."""
    _require_configured()
    session = oidc.open_session(
        settings.token_encryption_key, request.cookies.get(SESSION_COOKIE, "")
    )
    if session is None or not session.get("actor"):
        return {"authenticated": False}
    return {"authenticated": True, "actor": session["actor"], "sub": session["sub"]}
