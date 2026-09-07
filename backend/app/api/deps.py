"""Shared FastAPI dependencies: DB session, admin-key guard, host auth."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Iterator, NoReturn

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import decrypt_token_secret
from app.core.throttle import client_ip, throttle
from app.db.base import SessionLocal
from app.models.models import AgentToken, Host

# How stale `agent_tokens.last_used_at` is allowed to get before a successful
# auth rewrites it. Every agent request would otherwise UPDATE the row (~1/min
# per host) purely to advance a timestamp only the admin token list reads.
_LAST_USED_MIN_INTERVAL = timedelta(minutes=5)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def _reject_401(ip: str, kind: str, detail: str) -> NoReturn:
    """Record the auth failure, wait out the throttle's backoff without
    blocking the event loop, then raise 401."""
    delay = throttle.record_failure(ip, kind=kind)
    if delay:
        await asyncio.sleep(delay)
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail)


async def require_admin_key(
    request: Request, x_admin_key: str = Header(..., alias="X-Admin-Key")
) -> None:
    ok = hmac.compare_digest(x_admin_key, settings.admin_key)
    if settings.admin_key_previous:
        ok |= hmac.compare_digest(x_admin_key, settings.admin_key_previous)
    ip = client_ip(request)
    if not ok:
        await _reject_401(ip, "admin-key", "invalid admin key")
    throttle.record_success(ip)  # clear any backoff earned by earlier typos


def _resolve_active_token(
    db: Session, tok: AgentToken | None, now: datetime
) -> Host | None:
    """The chain shared by both auth paths, in order:

        token row exists (else None)
              -> not revoked, not expired (else None)
              -> hosts row by host_id
              -> hosts.is_active (else None)     <- decisive, whatever the token

    A disabled host is rejected even with a valid token, an active host is
    rejected with a revoked/expired one. Returns the host on success; the
    caller turns None into the actual 401 (each path wants a different
    "kind" for the throttle)."""
    if (
        tok is None
        or tok.revoked_at is not None
        or (tok.expires_at is not None and tok.expires_at <= now)
    ):
        return None
    host = db.get(Host, tok.host_id)
    if host is None or not host.is_active:
        return None
    return host


async def _auth_bearer(
    db: Session, ip: str, authorization: str, now: datetime
) -> tuple[Host, AgentToken]:
    """Legacy path: the raw token is sent as a bearer value every request,
    hashed and looked up. Kept for agents older than the signed-request
    protocol (docs/decisions.md "Authentication" -- transition sequence)."""
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        await _reject_401(ip, "bearer", "invalid authorization header")

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    tok = db.execute(
        select(AgentToken).where(AgentToken.token_hash == token_hash)
    ).scalar_one_or_none()
    host = _resolve_active_token(db, tok, now)
    if host is None:
        await _reject_401(ip, "bearer", "invalid token")
    return host, tok


async def _auth_signed(
    request: Request,
    db: Session,
    ip: str,
    token_hash_hdr: str,
    timestamp_hdr: str,
    signature_hdr: str,
    now: datetime,
) -> tuple[Host, AgentToken]:
    """New path: the raw token never crosses the wire. The agent sends the
    same SHA-256 hash already stored in token_hash (a non-secret lookup key
    it derives itself -- it reveals nothing usable without the real secret),
    a timestamp, and an HMAC-SHA256 over `timestamp\\nMETHOD\\npath\\nsha256(body)`
    keyed with the real secret. Verifying it needs that real secret, which is
    why it must be recoverable (Fernet, app.core.crypto), not just hashed."""
    tok = db.execute(
        select(AgentToken).where(AgentToken.token_hash == token_hash_hdr.strip().lower())
    ).scalar_one_or_none()
    host = _resolve_active_token(db, tok, now)
    if host is None:
        await _reject_401(ip, "signed", "invalid token")

    secret = tok.secret_encrypted and decrypt_token_secret(tok.secret_encrypted)
    if not secret:
        # Issued before this scheme existed (or never rotated onto it): its
        # plaintext was never stored, so it can never verify a signature.
        await _reject_401(ip, "signed", "token cannot verify signed requests")

    try:
        presented_ts = int(timestamp_hdr)
    except ValueError:
        await _reject_401(ip, "signed", "invalid timestamp")
    if abs(now.timestamp() - presented_ts) > settings.signature_window_seconds:
        await _reject_401(ip, "signed", "stale timestamp")

    body = await request.body()
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = f"{timestamp_hdr}\n{request.method}\n{request.url.path}\n{body_hash}"
    expected = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_hdr.strip().lower()):
        await _reject_401(ip, "signed", "invalid signature")

    return host, tok


async def get_current_host(
    request: Request,
    authorization: str | None = Header(None, alias="Authorization"),
    x_cadence_token_hash: str | None = Header(None, alias="X-Cadence-Token-Hash"),
    x_cadence_timestamp: str | None = Header(None, alias="X-Cadence-Timestamp"),
    x_cadence_signature: str | None = Header(None, alias="X-Cadence-Signature"),
    db: Session = Depends(get_db),
) -> Host:
    """Authenticate an agent request, one of two ways: the legacy bearer
    header, or the new signed-request headers (X-Cadence-Token-Hash /
    -Timestamp / -Signature). All three signed headers must be present
    together or none at all -- a partial set is rejected outright rather
    than silently falling back to bearer, so stripping one can never quietly
    downgrade a request. Neither form present is a 401, same as an invalid
    one (previously a 422 from a required Authorization header; see
    tests/test_reports.py -- deliberate, this endpoint now accepts either
    credential shape, so "sent none" is an authentication failure like any
    other, not a malformed request)."""
    ip = client_ip(request)
    now = datetime.now(timezone.utc)
    signed_headers = (x_cadence_token_hash, x_cadence_timestamp, x_cadence_signature)
    signed_present = [h is not None for h in signed_headers]

    if all(signed_present):
        host, tok = await _auth_signed(
            request, db, ip, x_cadence_token_hash, x_cadence_timestamp, x_cadence_signature, now
        )
        request.state.auth_scheme = "signed"
    elif any(signed_present):
        await _reject_401(ip, "signed", "incomplete signed-request headers")
    elif authorization is not None:
        host, tok = await _auth_bearer(db, ip, authorization, now)
        request.state.auth_scheme = "bearer"
    else:
        await _reject_401(ip, "bearer", "missing authorization")

    throttle.record_success(ip)  # clear any backoff earned by earlier failures
    # Opportunistic, rides the request's own commit -- but only when it has
    # drifted past the resolution we care about, to keep this off the hot path.
    if tok.last_used_at is None or now - tok.last_used_at >= _LAST_USED_MIN_INTERVAL:
        tok.last_used_at = now
    return host
