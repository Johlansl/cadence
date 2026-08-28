"""Admin provisioning endpoints, guarded by the X-Admin-Key shared secret."""

from __future__ import annotations

import hashlib
import secrets

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin_key
from app.models.models import Host
from app.schemas.schemas import HostCreate, HostCreated

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin_key)],
)


@router.post("/hosts", response_model=HostCreated, status_code=status.HTTP_201_CREATED)
def create_host(payload: HostCreate, db: Session = Depends(get_db)) -> HostCreated:
    # Generate the agent token; only its sha256 hash is ever stored.
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    host = Host(
        hostname=payload.hostname,
        fqdn=payload.fqdn,
        description=payload.description,
        token_hash=token_hash,
    )
    db.add(host)
    db.commit()
    db.refresh(host)

    return HostCreated(id=host.id, hostname=host.hostname, token=token)
