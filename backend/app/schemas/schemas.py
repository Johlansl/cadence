"""Pydantic request/response models for the V1 API."""

from __future__ import annotations

import enum
import re
import uuid
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import settings

# What the agent does after an upgrade that leaves a reboot pending.
#   auto   -- reboot immediately
#   never  -- leave it, operator handles reboots out of band
#   prompt -- leave it, but the dashboard offers a one-click reboot job
RebootMode = Literal["auto", "never", "prompt"]

# Job kinds the API accepts. The scheduler only ever creates 'apt_upgrade'.
JobType = Literal["apt_upgrade", "reboot"]

# Scope of a package_exclusions rule: every host, or one named host. A
# genuinely small, closed set (unlike JobType/failure category), gets a DB
# CHECK (migration 0015). No tag scope yet -- roadmap item 6.
PolicyScope = Literal["global", "host"]

# Event names a webhook can subscribe to. 'webhook.test' is deliberately not
# here: it is never subscribable, only ever sent to one explicitly targeted
# endpoint. Campaign events will be added later; the DB column stays free text
# so that needs no migration.
WebhookEventType = Literal[
    "job.succeeded",
    "job.failed",
    "host.offline",
    "host.reboot_required",
    "host.security_updates_available",
]


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
    """Admin-editable host settings (PATCH /api/v1/admin/hosts/{id}). All
    fields optional -- send only what changes."""

    reboot_policy: RebootMode | None = None
    is_active: bool | None = None
    # Free-form {key: value} labels. Groundwork for host groups in a later
    # version; today the dashboard only displays and filters on them.
    tags: dict[str, str] | None = None

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, v: dict | None) -> dict | None:
        if v is None:
            return v
        if len(v) > 20:
            raise ValueError("at most 20 tags per host")
        for key, value in v.items():
            if not key or len(key) > 40:
                raise ValueError("tag keys must be 1-40 characters")
            if len(value) > 80:
                raise ValueError("tag values must be at most 80 characters")
        return v


class HostPatched(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    hostname: str
    reboot_policy: str
    is_active: bool
    tags: dict[str, str]


# --- admin: agent tokens -------------------------------------------------------

class TokenCreate(BaseModel):
    """Issue an additional agent token for a host."""

    label: str | None = Field(default=None, max_length=80)
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def _must_be_future(cls, v: datetime | None) -> datetime | None:
        if v is None:
            return v
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        if v <= datetime.now(timezone.utc):
            raise ValueError("expires_at must be in the future")
        return v


class TokenIssued(BaseModel):
    id: int
    label: str | None
    expires_at: datetime | None
    # Plaintext token, returned exactly once at issue time.
    token: str


TokenState = Literal["active", "expired", "revoked"]


class TokenOut(BaseModel):
    """One agent token, hash never included. `state` is derived at read time
    from revoked_at / expires_at."""

    id: int
    label: str | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    state: TokenState


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
    # Debian source package name (agent 0.7.0+); used to link advisories.
    source_package: str | None = None


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    agent_version: str | None = None
    hostname: str = Field(min_length=1)
    fqdn: str | None = None
    os_family: str | None = "debian"
    os_name: str | None = None
    os_version: str | None = None
    # Debian release codename, e.g. "bookworm" (agent 0.7.0+).
    os_codename: str | None = None
    package_manager: str | None = "apt"
    reboot_required: bool = False
    packages: list[ReportPackage] = Field(default_factory=list)

    @field_validator("packages")
    @classmethod
    def _cap_packages(cls, v: list[ReportPackage]) -> list[ReportPackage]:
        cap = settings.max_report_packages
        if cap and len(v) > cap:
            raise ValueError(f"at most {cap} packages per report")
        return v


class JobHandoff(BaseModel):
    """A pending job handed to the agent -- in the report response (piggyback)
    or from the dedicated POST /api/v1/agent/next-job poll."""

    id: uuid.UUID
    job_type: str
    params: dict


class NextJob(BaseModel):
    job: JobHandoff | None = None


class ReportSummary(BaseModel):
    """One past report's counters (GET /hosts/{id}/reports) -- no raw_payload."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    received_at: datetime
    agent_version: str | None
    installed_package_count: int
    updates_available_count: int
    security_updates_count: int
    reboot_required: bool


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
    tags: dict[str, str]
    last_seen_at: datetime | None
    created_at: datetime
    updated_at: datetime
    reboot_policy: str
    # Derived from the host's current package state.
    status: HostStatus
    updates_available_count: int
    security_updates_count: int
    # How many of updates_available_count a package_exclusions rule matches;
    # always recomputed at read time, never stored (roadmap item 3).
    excluded_count: int


class FleetSummary(BaseModel):
    """Aggregates for the dashboard overview (GET /api/v1/fleet/summary).
    Host counts are over active hosts only; a host that has never reported is
    counted only in `silent` (no known status)."""

    total_hosts: int
    active_hosts: int
    inactive_hosts: int
    up_to_date: int
    updates_available: int
    security_updates_available: int
    reboot_required: int
    late: int
    silent: int
    pending_updates: int
    security_updates: int
    oldest_report_age_seconds: int | None
    jobs_running: int
    jobs_succeeded_24h: int
    jobs_failed_24h: int


class AdvisoryRef(BaseModel):
    """A security advisory linked to a pending security update. `id` is a
    DSA/DLA identifier; `url` points at the security-tracker page."""

    id: str
    url: str
    cves: list[str]


class HostPackageOut(BaseModel):
    name: str
    architecture: str
    installed_version: str
    candidate_version: str | None
    is_security_update: bool
    update_origin: str | None
    updated_at: datetime
    source_package: str | None = None
    advisories: list[AdvisoryRef] = []
    excluded: bool = False


class HostDetail(HostSummary):
    packages: list[HostPackageOut]


# --- cross-package view ---------------------------------------------------

class PackageHostOut(BaseModel):
    host_id: uuid.UUID
    hostname: str
    installed_version: str
    candidate_version: str | None
    is_security_update: bool
    update_origin: str | None
    updated_at: datetime
    source_package: str | None = None
    advisories: list[AdvisoryRef] = []


class PackageSummary(BaseModel):
    name: str
    architecture: str
    hosts: list[PackageHostOut]


# --- jobs ----------------------------------------------------------------

class JobCreate(BaseModel):
    job_type: JobType = "apt_upgrade"
    params: dict = Field(default_factory=dict)
    requested_by: str | None = None

    @field_validator("params")
    @classmethod
    def _validate_reboot_override(cls, v: dict) -> dict:
        # Optional per-job override of the host's reboot_policy.
        reboot = v.get("reboot")
        if reboot is not None and reboot not in ("auto", "never", "prompt"):
            raise ValueError("params.reboot must be 'auto', 'never' or 'prompt'")
        return v


class JobResultIn(BaseModel):
    """Posted by the agent once it has run the job."""

    status: Literal["succeeded", "failed"]
    exit_code: int
    log: str = ""
    reboot_required: bool | None = None
    # Sent by agent >= 0.9.0 for a failed job. Optional: an older agent omits
    # them and the columns stay NULL. Agent-authoritative, not constrained here.
    failure_category: str | None = None
    failure_summary: str | None = None
    # Sent by agent >= 0.10.0 for an apt_upgrade job, success or failure
    # (roadmap item 3): [] means holds were reconciled and nothing was
    # affected, None means not applicable (a reboot job, or an older agent).
    held_conflicts: list[str] | None = None
    # Sent by agent >= 0.10.1 for an apt_upgrade job (roadmap item 3 follow-up):
    # what Cadence actually holds on the host after this run's reconciliation.
    # None = not applicable; [] = reconciled, nothing held. Becomes the next
    # job's known_held_packages (app.exclusions.known_held_for_host).
    held_packages: list[str] | None = None


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
    failure_category: str | None
    failure_summary: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


# --- webhooks ----------------------------------------------------------------


def _validate_webhook_url(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("url must not be empty")
    parsed = urlsplit(v)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("url must be an absolute http(s) URL")
    return v


class WebhookCreate(BaseModel):
    """Register an outbound webhook. The signing secret is generated server
    side and returned once in WebhookCreated."""

    url: str
    event_types: list[WebhookEventType] = Field(min_length=1)
    enabled: bool = True
    description: str | None = Field(default=None, max_length=200)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return _validate_webhook_url(v)

    @field_validator("event_types")
    @classmethod
    def _dedup(cls, v: list[str]) -> list[str]:
        seen: dict[str, None] = {}
        for item in v:
            seen.setdefault(item, None)
        return list(seen)


class WebhookUpdate(BaseModel):
    """Partial update. The route merges these onto the row and re-validates the
    result as a WebhookCreate."""

    url: str | None = None
    event_types: list[WebhookEventType] | None = None
    enabled: bool | None = None
    description: str | None = None


class WebhookCreated(BaseModel):
    """The only response that carries the full url and the plaintext secret,
    returned exactly once at creation."""

    id: uuid.UUID
    url: str
    secret: str
    enabled: bool
    event_types: list[str]
    description: str | None
    created_at: datetime


class WebhookOut(BaseModel):
    """A webhook as shown on the dashboard. `url_preview` is masked; the raw
    url and the secret are never returned here. `last_success_at` / `last_error`
    / `*_count` are derived from webhook_deliveries at read time."""

    id: uuid.UUID
    url_preview: str
    enabled: bool
    event_types: list[str]
    description: str | None
    created_at: datetime
    updated_at: datetime
    last_success_at: datetime | None
    last_error: str | None
    pending_count: int
    failed_count: int


class WebhookTestAccepted(BaseModel):
    delivery_id: uuid.UUID


# --- package exclusions -------------------------------------------------------

# A conservative glob charset: package names and fnmatch wildcards, no shell
# metacharacters that would suggest the operator pasted something else.
_PATTERN_RE = re.compile(r"^[A-Za-z0-9+.*?_-]+$")


class PackageExclusionCreate(BaseModel):
    """A new hold rule. `host_id` is required iff scope='host', forbidden
    otherwise -- validated here (early 422) and again by the DB CHECK."""

    scope: PolicyScope
    host_id: uuid.UUID | None = None
    pattern: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=200)

    @field_validator("pattern")
    @classmethod
    def _pattern(cls, v: str) -> str:
        v = v.strip()
        if not v or not _PATTERN_RE.match(v):
            raise ValueError(
                "pattern must be a package name or glob "
                "(letters, digits, + . * ? _ -)"
            )
        return v

    @model_validator(mode="after")
    def _scope_host(self) -> "PackageExclusionCreate":
        if self.scope == "global" and self.host_id is not None:
            raise ValueError("host_id must not be set when scope='global'")
        if self.scope == "host" and self.host_id is None:
            raise ValueError("host_id is required when scope='host'")
        return self


class PackageExclusionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scope: PolicyScope
    host_id: uuid.UUID | None
    pattern: str
    description: str | None
    created_at: datetime


# --- schedules -----------------------------------------------------------

ScheduleKind = Literal["monthly", "weekly"]


class ScheduleIn(BaseModel):
    """Create / full replacement of a host's maintenance window. Mirrors the
    DB CHECKs so the API returns a friendly 422 instead of an IntegrityError."""

    enabled: bool = True
    kind: ScheduleKind
    day_of_month: int | None = Field(default=None, ge=1, le=28)
    weekday: int | None = Field(default=None, ge=0, le=6)  # Monday = 0
    hour: int = Field(ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    timezone: str = "UTC"
    params: dict = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def _known_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {v!r}") from exc
        return v

    @model_validator(mode="after")
    def _coherent(self) -> "ScheduleIn":
        if self.kind == "monthly" and (self.day_of_month is None or self.weekday is not None):
            raise ValueError("monthly needs day_of_month and no weekday")
        if self.kind == "weekly" and (self.weekday is None or self.day_of_month is not None):
            raise ValueError("weekly needs weekday and no day_of_month")
        reboot = self.params.get("reboot")
        if reboot is not None and reboot not in ("auto", "never", "prompt"):
            raise ValueError("params.reboot must be 'auto', 'never' or 'prompt'")
        return self


class ScheduleUpdate(BaseModel):
    """Partial update -- the route merges these onto the row and re-validates
    the result as a ScheduleIn."""

    enabled: bool | None = None
    kind: ScheduleKind | None = None
    day_of_month: int | None = None
    weekday: int | None = None
    hour: int | None = None
    minute: int | None = None
    timezone: str | None = None
    params: dict | None = None


class ScheduleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    host_id: uuid.UUID
    enabled: bool
    kind: str
    day_of_month: int | None
    weekday: int | None
    hour: int
    minute: int
    timezone: str
    params: dict
    last_run_at: datetime | None
    next_run_at: datetime | None
    created_at: datetime
    updated_at: datetime


# --- audit -------------------------------------------------------------------


class AuditEntry(BaseModel):
    """One row of the admin audit trail (GET /api/v1/admin/audit)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    at: datetime
    action: str
    target_type: str | None
    target_id: str | None
    actor: str
    client: str | None
    request_id: str | None
    detail: dict | None
