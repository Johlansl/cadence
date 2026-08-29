"""Pydantic request/response models for the V1 API."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# What the agent does after an upgrade that leaves a reboot pending.
RebootMode = Literal["auto", "never"]


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


class HostUpdate(BaseModel):
    """Admin-editable host settings (PATCH /api/v1/admin/hosts/{id})."""

    reboot_policy: RebootMode


class HostPatched(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    hostname: str
    reboot_policy: str


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


class JobHandoff(BaseModel):
    """A pending job handed to the agent -- in the report response (piggyback)
    or from the dedicated POST /api/v1/agent/next-job poll."""

    id: uuid.UUID
    job_type: str
    params: dict


class NextJob(BaseModel):
    job: JobHandoff | None = None


class ReportAccepted(BaseModel):
    host_id: uuid.UUID
    installed_package_count: int
    updates_available_count: int
    security_updates_count: int
    reboot_required: bool
    # Set when a pending job was picked up for this host.
    job: JobHandoff | None = None


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
    reboot_policy: str
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


# --- jobs ----------------------------------------------------------------

class JobCreate(BaseModel):
    job_type: str = "apt_upgrade"
    params: dict = Field(default_factory=dict)
    requested_by: str | None = None

    @field_validator("params")
    @classmethod
    def _validate_reboot_override(cls, v: dict) -> dict:
        # Optional per-job override of the host's reboot_policy.
        reboot = v.get("reboot")
        if reboot is not None and reboot not in ("auto", "never"):
            raise ValueError("params.reboot must be 'auto' or 'never'")
        return v


class JobResultIn(BaseModel):
    """Posted by the agent once it has run the job."""

    status: Literal["succeeded", "failed"]
    exit_code: int
    log: str = ""
    reboot_required: bool | None = None


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    host_id: uuid.UUID
    job_type: str
    status: str
    params: dict
    requested_by: str | None
    result: dict | None
    log: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
