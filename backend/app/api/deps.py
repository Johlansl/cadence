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
from app.core.ratelimit import note_rejected, ratelimiter
from app.core.throttle import client_ip, throttle
from app.db.base import SessionLocal
from app.models.models import AgentCertificate, AgentToken, Host

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


def _enforce_rate_limit(
    request: Request, key: str, *, limit: int, surface: str, key_label: str
) -> None:
    """Count one successful authenticated request and raise 429 (with a
    Retry-After header) if `key` is over `limit` for the configured window.
    Volume cap only; auth failures are handled by _reject_401 / the throttle."""
    retry_after = ratelimiter.check(
        key, limit=limit, window=settings.ratelimit_window_seconds
    )
    if retry_after:
        note_rejected(surface, key_label, limit, retry_after, request.url.path)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate limit exceeded",
            headers={"Retry-After": str(int(retry_after))},
        )


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
    _enforce_rate_limit(
        request, f"admin:{ip}", limit=settings.ratelimit_admin_max,
        surface="admin", key_label=ip,
    )


def _resolve_active_token(
    db: Session, tok: AgentToken | None, now: datetime
) -> Host | None:
    """The token/host validity chain for the signed auth path, in order:

        token row exists (else None)
              -> not revoked, not expired (else None)
              -> hosts row by host_id
              -> hosts.is_active (else None)     <- decisive, whatever the token

    A disabled host is rejected even with a valid token, an active host is
    rejected with a revoked/expired one. Returns the host on success; the
    caller turns None into the actual 401."""
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


async def _auth_signed(
    request: Request,
    db: Session,
    ip: str,
    token_hash_hdr: str,
    timestamp_hdr: str,
    signature_hdr: str,
    now: datetime,
) -> tuple[Host, AgentToken]:
    """The raw token never crosses the wire. The agent sends the same SHA-256
    hash already stored in token_hash (a non-secret lookup key it derives
    itself -- it reveals nothing usable without the real secret), a timestamp,
    and an HMAC-SHA256 over `timestamp\\nMETHOD\\npath\\nsha256(body)` keyed
    with the real secret. Verifying it needs that real secret, which is why it
    must be recoverable (Fernet, app.core.crypto), not just hashed."""
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


async def _auth_transport(
    request: Request,
    db: Session,
    host: Host,
    ip: str,
    now: datetime,
    transport: str | None,
    proxy_key: str | None,
    certificate_fingerprint: str | None,
) -> str:
    """Bind Caddy's authenticated transport identity to the HMAC host."""
    if not settings.require_agent_transport_auth:
        return "signed"
    if proxy_key is None or not hmac.compare_digest(proxy_key, settings.internal_proxy_key):
        await _reject_401(ip, "transport", "invalid agent transport")
    if transport == "legacy":
        if not settings.legacy_agent_endpoints:
            await _reject_401(ip, "transport", "invalid agent transport")
        return "signed+legacy"
    if transport != "mtls" or certificate_fingerprint is None:
        await _reject_401(ip, "transport", "invalid agent transport")

    fingerprint = certificate_fingerprint.strip().lower()
    if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
        await _reject_401(ip, "transport", "invalid agent transport")
    certificate = db.execute(
        select(AgentCertificate).where(
            AgentCertificate.fingerprint_sha256 == fingerprint
        )
    ).scalar_one_or_none()
    if (
        certificate is None
        or certificate.host_id != host.id
        or certificate.revoked_at is not None
        or certificate.not_before > now
        or certificate.expires_at <= now
    ):
        await _reject_401(ip, "transport", "invalid agent transport")
    if (
        certificate.last_used_at is None
        or now - certificate.last_used_at >= _LAST_USED_MIN_INTERVAL
    ):
        certificate.last_used_at = now
    return "signed+mtls"


async def get_current_host(
    request: Request,
    x_cadence_token_hash: str | None = Header(None, alias="X-Cadence-Token-Hash"),
    x_cadence_timestamp: str | None = Header(None, alias="X-Cadence-Timestamp"),
    x_cadence_signature: str | None = Header(None, alias="X-Cadence-Signature"),
    x_cadence_transport: str | None = Header(None, alias="X-Cadence-Transport"),
    x_cadence_proxy_key: str | None = Header(None, alias="X-Cadence-Proxy-Key"),
    x_cadence_client_fingerprint: str | None = Header(
        None, alias="X-Cadence-Client-Cert-Fingerprint"
    ),
    db: Session = Depends(get_db),
) -> Host:
    """Authenticate an agent request from the signed-request headers
    (X-Cadence-Token-Hash / -Timestamp / -Signature). All three must be
    present together or none at all -- a partial set is rejected outright
    with its own message, so stripping one can never quietly change how the
    request is read. No headers at all is a 401, the same as an invalid
    signature: this is an authentication failure, not a malformed request."""
    ip = client_ip(request)
    now = datetime.now(timezone.utc)
    signed_headers = (x_cadence_token_hash, x_cadence_timestamp, x_cadence_signature)
    signed_present = [h is not None for h in signed_headers]

    if all(signed_present):
        host, tok = await _auth_signed(
            request, db, ip, x_cadence_token_hash, x_cadence_timestamp, x_cadence_signature, now
        )
        request.state.auth_scheme = await _auth_transport(
            request,
            db,
            host,
            ip,
            now,
            x_cadence_transport,
            x_cadence_proxy_key,
            x_cadence_client_fingerprint,
        )
    elif any(signed_present):
        await _reject_401(ip, "signed", "incomplete signed-request headers")
    else:
        await _reject_401(ip, "signed", "missing signed-request headers")

    throttle.record_success(ip)  # clear any backoff earned by earlier failures
    _enforce_rate_limit(
        request, tok.token_hash, limit=settings.ratelimit_agent_max,
        surface="agent", key_label=tok.token_hash[:8],
    )
    # Opportunistic, rides the request's own commit -- but only when it has
    # drifted past the resolution we care about, to keep this off the hot path.
    if tok.last_used_at is None or now - tok.last_used_at >= _LAST_USED_MIN_INTERVAL:
        tok.last_used_at = now
    return host
