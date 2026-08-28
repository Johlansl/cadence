"""Pydantic request/response models for the V1 API."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# --- admin: host provisioning -------------------------------------------------

class HostCreate(BaseModel):
    hostname: str = Field(min_length=1)
    fqdn: str | None = None
    description: str | None = None


class HostCreated(BaseModel):
    id: uuid.UUID
    hostname: str
    # Plaintext token, returned exactly once at creation time.
    token: str


# --- agent report ingestion -------------------------------------------------

class ReportPackage(BaseModel):
    # Keep unknown fields so the raw report log stays faithful.
    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1)
    architecture: str = ""
    installed_version: str
    candidate_version: str | None = None
    is_security_update: bool = False
    update_origin: str | None = None


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    agent_version: str | None = None
    hostname: str = Field(min_length=1)
    fqdn: str | None = None
    os_family: str | None = "debian"
    os_name: str | None = None
    os_version: str | None = None
    package_manager: str | None = "apt"
    reboot_required: bool = False
    packages: list[ReportPackage] = Field(default_factory=list)


class ReportAccepted(BaseModel):
    host_id: uuid.UUID
    installed_package_count: int
    updates_available_count: int
    security_updates_count: int
    reboot_required: bool


# --- host views -----------------------------------------------------------

class HostStatus(str, enum.Enum):
    up_to_date = "up_to_date"
    updates_available = "updates_available"
    security_updates_available = "security_updates_available"


class HostSummary(BaseModel):
    id: uuid.UUID
    hostname: str
    fqdn: str | None
    description: str | None
    os_family: str
    os_name: str | None
    os_version: str | None
    package_manager: str
    agent_version: str | None
    reboot_required: bool
    is_active: bool
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # Derived from the host's current package state.
    status: HostStatus
    updates_available_count: int
    security_updates_count: int


class HostPackageOut(BaseModel):
    name: str
    architecture: str
    installed_version: str
    candidate_version: str | None
    is_security_update: bool
    update_origin: str | None
    updated_at: datetime


class HostDetail(HostSummary):
    packages: list[HostPackageOut]
