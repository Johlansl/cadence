"""ORM models mapping the Cadence database schema.

The schema is authoritative and owned by the Alembic migration chain
(`backend/alembic/versions/`, baseline `0001` mirrors the historical
`app/db/init.sql`). These classes must stay in sync with it; no DDL is
emitted from the ORM. Indexes and CHECK constraints live only in the
migrations, not here.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Host(Base):
    __tablename__ = "hosts"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    hostname: Mapped[str] = mapped_column(Text, nullable=False)
    fqdn: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    os_family: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'debian'"))
    os_name: Mapped[str | None] = mapped_column(Text)
    os_version: Mapped[str | None] = mapped_column(Text)
    # Debian release codename ('bookworm'); agent 0.7.0+. Preferred over the
    # os_version -> codename inference when set. See migration 0011.
    os_codename: Mapped[str | None] = mapped_column(Text)
    package_manager: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'apt'"))
    agent_version: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    # What the agent does after an upgrade that needs a reboot: 'auto' | 'never'
    # (DB CHECK). A per-job override lives in jobs.params->>'reboot'.
    reboot_policy: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'never'")
    )
    reboot_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AgentToken(Base):
    """An authentication token for one host's agent. Several may be active at
    once so a token can be rotated without downtime. State is derived from
    revoked_at / expires_at only -- no boolean flag that could drift. Nothing
    here touches jobs: revoking a token is an auth-plane change, it does not
    cancel jobs. See migration 0008."""

    __tablename__ = "agent_tokens"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    host_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Fernet-encrypted copy of the plaintext, so a signed request can be
    # verified (needs the real secret, not a one-way hash). NULL for tokens
    # issued before this existed -- see migration 0012.
    secret_encrypted: Mapped[str | None] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Package(Base):
    __tablename__ = "packages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    architecture: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))


class HostPackage(Base):
    __tablename__ = "host_packages"

    host_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("hosts.id", ondelete="CASCADE"), primary_key=True
    )
    package_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("packages.id", ondelete="CASCADE"), primary_key=True
    )
    installed_version: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_version: Mapped[str | None] = mapped_column(Text)
    is_security_update: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    update_origin: Mapped[str | None] = mapped_column(Text)
    # Debian source package name for this binary; agent 0.7.0+. Preferred over
    # the curated binary->source map when set. See migration 0011.
    source_package: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Advisory(Base):
    """One upstream security advisory (Debian DSA/DLA now, Ubuntu USN later).
    Populated only by the scheduler's advisory-refresh task; see migration
    0010. `cve_ids` is a JSON array of "CVE-YYYY-NNNN" strings."""

    __tablename__ = "advisories"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    cve_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdvisoryPackage(Base):
    """A (release, source package) that a given advisory fixes, with the
    version the fix landed in. The read API joins a host's pending security
    updates against `(package, release)` and keeps rows whose `fixed_version`
    matches apt's candidate. See migration 0010."""

    __tablename__ = "advisory_packages"

    advisory_id: Mapped[str] = mapped_column(
        Text, ForeignKey("advisories.id", ondelete="CASCADE"), primary_key=True
    )
    release: Mapped[str] = mapped_column(Text, primary_key=True)
    package: Mapped[str] = mapped_column(Text, primary_key=True)
    fixed_version: Mapped[str] = mapped_column(Text, nullable=False)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    host_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    agent_version: Mapped[str | None] = mapped_column(Text)
    installed_package_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    updates_available_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    security_updates_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    reboot_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)


class Job(Base):
    __tablename__ = "jobs"

    # status is free text on purpose (extensibility); the values used in V1 are
    # pending -> running -> succeeded | failed.
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    host_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False
    )
    job_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'apt_upgrade'")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    params: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    requested_by: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column(JSONB)
    log: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Schedule(Base):
    __tablename__ = "schedules"

    # One recurring maintenance window per host. Coherence (kind vs
    # day_of_month/weekday, ranges, params.reboot) is enforced by DB CHECKs;
    # see migration 0003. weekday: Monday = 0.
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    host_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    day_of_month: Mapped[int | None] = mapped_column(SmallInteger)
    weekday: Mapped[int | None] = mapped_column(SmallInteger)
    hour: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    minute: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'UTC'"))
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuditLog(Base):
    """Append-only trail of successful admin writes. One row per mutating
    X-Admin-Key call, staged on the handler's session and committed in the
    same transaction as the change (migration 0007). No FK on target_id so a
    row survives the deletion of the host / schedule / job it describes.
    `action` is free text ("resource.verb")."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'admin'"))
    client: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSONB)


class SchedulerState(Base):
    """Small key/value store for the scheduler process. Currently just the
    timestamp of the last retention sweep, so a restart doesn't re-run it.
    See migration 0005."""

    __tablename__ = "scheduler_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
