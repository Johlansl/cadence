"""Shared FastAPI dependencies: DB session, admin-key guard, host auth."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from typing import Iterator, NoReturn

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.throttle import client_ip, throttle
from app.db.base import SessionLocal
from app.models.models import Host


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
    if not ok:
        await _reject_401(client_ip(request), "admin-key", "invalid admin key")


async def get_current_host(
    request: Request,
    authorization: str = Header(..., alias="Authorization"),
    db: Session = Depends(get_db),
) -> Host:
    ip = client_ip(request)
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        await _reject_401(ip, "bearer", "invalid authorization header")

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    host = db.execute(
        select(Host).where(Host.token_hash == token_hash)
    ).scalar_one_or_none()
    if host is None or not host.is_active:
        await _reject_401(ip, "bearer", "invalid token")
    return host
