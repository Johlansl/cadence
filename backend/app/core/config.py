"""Runtime configuration, read from environment variables (no config file in V1)."""

from __future__ import annotations

import os
from urllib.parse import quote


def _build_database_url() -> str:
    explicit = os.environ.get("CADENCE_DATABASE_URL")
    if explicit:
        return explicit
    user = os.environ.get("POSTGRES_USER", "cadence")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    name = os.environ.get("POSTGRES_DB", "cadence")
    host = os.environ.get("POSTGRES_HOST", "db")
    port = os.environ.get("POSTGRES_PORT", "5432")
    return (
        f"postgresql+psycopg2://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{name}"
    )


def _non_negative_int(var: str, default: int) -> int:
    """A non-negative integer env var. 0 has a per-setting meaning (keep
    forever / feature disabled). Read by the scheduler."""
    raw = os.environ.get(var, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{var} must be an integer, got {raw!r}") from exc
    if value < 0:
        raise RuntimeError(f"{var} must be >= 0")
    return value


class Settings:
    def __init__(self) -> None:
        self.database_url: str = _build_database_url()
        # Shared secret guarding the admin provisioning endpoints (X-Admin-Key).
        self.admin_key: str = os.environ.get("CADENCE_ADMIN_KEY", "")
        if not self.admin_key:
            raise RuntimeError("CADENCE_ADMIN_KEY is required")

        # Retention, applied by the scheduler's daily sweep. 0 = keep forever.
        self.reports_retention_days: int = _non_negative_int(
            "CADENCE_REPORTS_RETENTION_DAYS", 90
        )
        self.jobs_retention_days: int = _non_negative_int(
            "CADENCE_JOBS_RETENTION_DAYS", 90
        )

        # A job left 'running' longer than this is failed by the scheduler's
        # reaper -- a dead agent would otherwise block every future job for
        # that host. 0 = disabled. Default 2h.
        self.job_running_timeout_seconds: int = _non_negative_int(
            "CADENCE_JOB_RUNNING_TIMEOUT_SECONDS", 7200
        )


settings = Settings()
