"""Shared FastAPI dependencies: DB session, admin-key guard, host auth."""

from __future__ import annotations

import hashlib
import hmac
from typing import Iterator

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


def require_admin_key(
    request: Request, x_admin_key: str = Header(..., alias="X-Admin-Key")
) -> None:
    if not hmac.compare_digest(x_admin_key, settings.admin_key):
        throttle.record_failure(client_ip(request), kind="admin-key")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid admin key")


def get_current_host(
    request: Request,
    authorization: str = Header(..., alias="Authorization"),
    db: Session = Depends(get_db),
) -> Host:
    ip = client_ip(request)
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        throttle.record_failure(ip, kind="bearer")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid authorization header")

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    host = db.execute(
        select(Host).where(Host.token_hash == token_hash)
    ).scalar_one_or_none()
    if host is None or not host.is_active:
        throttle.record_failure(ip, kind="bearer")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    return host
