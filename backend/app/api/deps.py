"""Shared FastAPI dependencies: DB session, admin-key guard, host auth."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from datetime import datetime, timezone
from typing import Iterator, NoReturn

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.throttle import client_ip, throttle
from app.db.base import SessionLocal
from app.models.models import AgentToken, Host


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


async def get_current_host(
    request: Request,
    authorization: str = Header(..., alias="Authorization"),
    db: Session = Depends(get_db),
) -> Host:
    """Authenticate an agent from its bearer token. One ordered chain:

        token -> agent_tokens row (else 401)
              -> not revoked, not expired (else 401)
              -> hosts row by host_id
              -> hosts.is_active (else 401)     <- decisive, whatever the token

    The host `is_active` gate is independent of token state: a disabled host
    is rejected even with a valid token, an active host is rejected with a
    revoked/expired one. Both must pass, here, in this order.
    """
    ip = client_ip(request)
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        await _reject_401(ip, "bearer", "invalid authorization header")

    now = datetime.now(timezone.utc)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    tok = db.execute(
        select(AgentToken).where(AgentToken.token_hash == token_hash)
    ).scalar_one_or_none()
    if (
        tok is None
        or tok.revoked_at is not None
        or (tok.expires_at is not None and tok.expires_at <= now)
    ):
        await _reject_401(ip, "bearer", "invalid token")

    host = db.get(Host, tok.host_id)
    if host is None or not host.is_active:
        await _reject_401(ip, "bearer", "invalid token")

    throttle.record_success(ip)  # clear any backoff earned by earlier failures
    tok.last_used_at = now  # opportunistic; rides the request's own commit
    return host
