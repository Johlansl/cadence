"""Audit trail for admin writes.

`record_audit()` stages one `audit_log` row on the caller's session -- it
does NOT commit. The handler commits it together with the mutation, so a row
exists only for a change that actually landed; a handler that raises before
its commit leaves nothing behind.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.core.throttle import client_ip
from app.models.models import AuditLog


def record_audit(
    db: Session,
    request: Request,
    action: str,
    *,
    target_type: str | None = None,
    target_id: Any | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Stage an audit row on `db` (no commit).

    - `action` is "resource.verb" (e.g. "host.create"), an open set.
    - `actor` comes from the optional `X-Actor` header (free text, no RBAC),
      falling back to 'admin' when absent or blank.
    - `client` is the throttle's XFF-aware caller IP, so it records the real
      client rather than the Caddy container's address.
    - `request_id` is the id the logging middleware put on `request.state`.
    - `target_id` is stringified so any id type (UUID, int) fits the column.
    """
    actor = (request.headers.get("x-actor") or "").strip() or "admin"
    db.add(
        AuditLog(
            action=action,
            target_type=target_type,
            target_id=None if target_id is None else str(target_id),
            actor=actor,
            client=client_ip(request),
            request_id=getattr(request.state, "request_id", None),
            detail=detail,
        )
    )
