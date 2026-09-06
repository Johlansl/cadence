"""Runtime configuration, read from environment variables (no config file in V1)."""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import quote

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


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


def _trusted_proxies(var: str) -> list[IPNetwork]:
    """Parse a comma/space-separated list of CIDRs or bare IPs. Empty by
    default: with no trusted proxy, X-Forwarded-For is not believed and the
    direct connection IP is used (fail-safe). Read by app.core.throttle."""
    raw = os.environ.get(var, "").replace(",", " ").split()
    nets: list[IPNetwork] = []
    for item in raw:
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError as exc:
            raise RuntimeError(f"{var}: {item!r} is not a valid CIDR or IP") from exc
    return nets


def _bool_env(var: str, default: bool) -> bool:
    """A boolean env var. Unset -> default; otherwise anything but a clear
    falsey token (0/false/no/off) counts as true."""
    raw = os.environ.get(var, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


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
        if len(self.admin_key) < 12:
            raise RuntimeError(
                "CADENCE_ADMIN_KEY must be at least 12 characters "
                "(use `openssl rand -hex 32`)"
            )
        # Optional second key accepted alongside CADENCE_ADMIN_KEY, so the key
        # can be rotated without a flag day: set the new key, move the old one
        # here, update clients, then drop it.
        self.admin_key_previous: str = os.environ.get("CADENCE_ADMIN_KEY_PREVIOUS", "")

        # Networks whose X-Forwarded-For header is trusted (the reverse
        # proxies in front of the backend). Empty -> the direct connection IP
        # is used for the auth throttle and the audit `client` column.
        self.trusted_proxies: list[IPNetwork] = _trusted_proxies("CADENCE_TRUSTED_PROXIES")

        # Retention, applied by the scheduler's daily sweep. 0 = keep forever.
        self.reports_retention_days: int = _non_negative_int(
            "CADENCE_REPORTS_RETENTION_DAYS", 90
        )
        self.jobs_retention_days: int = _non_negative_int(
            "CADENCE_JOBS_RETENTION_DAYS", 90
        )
        # The admin audit trail is small and worth keeping longer. 0 = forever.
        self.audit_retention_days: int = _non_negative_int(
            "CADENCE_AUDIT_RETENTION_DAYS", 365
        )
        # Revoked/expired agent tokens are dead weight once no one needs the
        # "when was this last used" history. Days since revocation/expiry
        # before a token row is deleted. 0 = keep forever.
        self.token_retention_days: int = _non_negative_int(
            "CADENCE_TOKEN_RETENTION_DAYS", 90
        )

        # A job left 'running' longer than this is failed by the scheduler's
        # reaper -- a dead agent would otherwise block every future job for
        # that host. 0 = disabled. Default 2h.
        self.job_running_timeout_seconds: int = _non_negative_int(
            "CADENCE_JOB_RUNNING_TIMEOUT_SECONDS", 7200
        )

        # POST /api/v1/reports payload caps. A report body is stored verbatim
        # in reports.raw_payload every cycle, so an unbounded one is a cheap
        # way for a leaked token to fill the disk. 0 = no limit.
        self.max_report_bytes: int = _non_negative_int(
            "CADENCE_MAX_REPORT_BYTES", 5 * 1024 * 1024
        )
        self.max_report_packages: int = _non_negative_int(
            "CADENCE_MAX_REPORT_PACKAGES", 10_000
        )

        # How long the container prestart waits for Postgres to accept
        # connections before giving up. 0 = try once. Read by app.prestart.
        self.db_wait_seconds: int = _non_negative_int("CADENCE_DB_WAIT_SECONDS", 60)

        # Security-advisory enrichment: the scheduler periodically pulls the
        # Debian DSA/DLA feeds. Disable (CI, air-gapped installs) with
        # CADENCE_ADVISORY_REFRESH_ENABLED=false. An empty URL list means "use
        # the built-in Debian defaults" (app.advisories.debian.DEFAULT_FEED_URLS).
        self.advisory_refresh_enabled: bool = _bool_env(
            "CADENCE_ADVISORY_REFRESH_ENABLED", True
        )
        self.advisory_feed_urls: list[str] = (
            os.environ.get("CADENCE_ADVISORY_FEED_URLS", "").replace(",", " ").split()
        )


settings = Settings()
