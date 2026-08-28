"""ORM models mapping the tables defined in app/db/init.sql.

The database schema is authoritative (applied via init.sql); these classes must
stay in sync with it. No DDL is emitted from Python in V1.
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
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    os_family: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'debian'"))
    os_name: Mapped[str | None] = mapped_column(Text)
    os_version: Mapped[str | None] = mapped_column(Text)
    package_manager: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'apt'"))
    agent_version: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


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
