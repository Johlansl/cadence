"""Runtime configuration, read from environment variables (no config file in V1)."""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import quote

from cryptography.fernet import Fernet

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


def _positive_int(var: str, default: int) -> int:
    """A strictly positive integer env var -- unlike _non_negative_int, 0 has
    no "disabled" meaning here and is rejected. Used for settings where 0
    would silently defeat the point of the setting (a default expiry of "0
    days" is not a safer default, it is the no-expiry gap this exists to
    close)."""
    raw = os.environ.get(var, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{var} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise RuntimeError(f"{var} must be > 0")
    return value


def _bounded_positive_int(var: str, default: int, maximum: int) -> int:
    """A positive integer with an inclusive upper safety bound."""
    value = _positive_int(var, default)
    if value > maximum:
        raise RuntimeError(f"{var} must be <= {maximum}")
    return value


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

        # Fernet key encrypting agent_tokens.secret_encrypted at rest, so a
        # signed request can be verified (needs the real secret, not a
        # one-way hash). Same class of secret as CADENCE_ADMIN_KEY -- a
        # plaintext credential in .env -- and rotated the same way: put the
        # new key here, move the old one to _PREVIOUS, update nothing else,
        # drop _PREVIOUS once every row using it has been re-encrypted.
        # Generate with:
        #   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
        self.token_encryption_key: str = os.environ.get("CADENCE_TOKEN_ENCRYPTION_KEY", "")
        if not self.token_encryption_key:
            raise RuntimeError("CADENCE_TOKEN_ENCRYPTION_KEY is required")
        try:
            Fernet(self.token_encryption_key.encode())
        except Exception as exc:
            raise RuntimeError(
                "CADENCE_TOKEN_ENCRYPTION_KEY is not a valid Fernet key (generate one with "
                '`python3 -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"`)'
            ) from exc
        self.token_encryption_key_previous: str = os.environ.get(
            "CADENCE_TOKEN_ENCRYPTION_KEY_PREVIOUS", ""
        )
        if self.token_encryption_key_previous:
            try:
                Fernet(self.token_encryption_key_previous.encode())
            except Exception as exc:
                raise RuntimeError(
                    "CADENCE_TOKEN_ENCRYPTION_KEY_PREVIOUS is not a valid Fernet key"
                ) from exc

        # New tokens expire this many days after issue unless the caller
        # passes an explicit expires_at. 0 is rejected (see _positive_int):
        # a default of "no expiry" is the gap this setting exists to close.
        self.token_default_expiry_days: int = _positive_int(
            "CADENCE_TOKEN_DEFAULT_EXPIRY_DAYS", 365
        )

        # How far a signed request's X-Cadence-Timestamp may drift from the
        # server's clock, either direction, before it is rejected. Bounds
        # clock-skew tolerance and how long a captured request stays
        # replayable; unrelated to how often the agent talks (~1 min poll,
        # ~30 min report) -- see docs/decisions.md "Authentication".
        self.signature_window_seconds: int = _positive_int(
            "CADENCE_SIGNATURE_WINDOW_SECONDS", 300
        )

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

        # Server-pinned thresholds handed to every apt_upgrade agent. Keeping
        # these values in job.params makes each run reproducible in the audit
        # trail. Agents retain the same defaults for compatibility with jobs
        # created by an older backend.
        self.upgrade_minimum_available_bytes: int = _positive_int(
            "CADENCE_UPGRADE_MINIMUM_AVAILABLE_BYTES", 1024 * 1024 * 1024
        )
        self.upgrade_boot_minimum_available_bytes: int = _positive_int(
            "CADENCE_UPGRADE_BOOT_MINIMUM_AVAILABLE_BYTES", 200 * 1024 * 1024
        )
        self.upgrade_lock_wait_seconds: int = _bounded_positive_int(
            "CADENCE_UPGRADE_LOCK_WAIT_SECONDS", 120, 3600
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

        # The shared `packages` dimension is get-or-created on every report and
        # never shrinks. A weekly scheduler sweep deletes rows no host_packages
        # references (every read path inner-joins from host_packages, so an
        # orphan is invisible anyway). Disable with
        # CADENCE_PACKAGES_GC_ENABLED=false.
        self.packages_gc_enabled: bool = _bool_env("CADENCE_PACKAGES_GC_ENABLED", True)

        # Volume cap on *successful* authenticated traffic (app.core.ratelimit),
        # separate from the auth-failure throttle (app.core.throttle): a valid
        # admin key or agent token is otherwise unlimited. Counted per fixed
        # window, keyed by resolved token_hash for the agent path and by client
        # IP for the admin and dashboard/read paths. Over the cap -> 429 with a
        # Retry-After header. No per-surface "0 = off" (0 is rejected like the
        # other _positive_int settings); CADENCE_RATELIMIT_ENABLED=false is the
        # single off switch.
        self.ratelimit_enabled: bool = _bool_env("CADENCE_RATELIMIT_ENABLED", True)
        self.ratelimit_window_seconds: int = _positive_int(
            "CADENCE_RATELIMIT_WINDOW_SECONDS", 60
        )
        # Per host token: real agent cadence is ~1 poll/min + a report every
        # ~30 min, so 20/min is ~20x steady with wide burst headroom.
        self.ratelimit_agent_max: int = _positive_int("CADENCE_RATELIMIT_AGENT_MAX", 20)
        # Per IP: human admin writes, bursty when BulkActionBar fans out one
        # write per selected host. 60/min covers a large bulk action.
        self.ratelimit_admin_max: int = _positive_int("CADENCE_RATELIMIT_ADMIN_MAX", 60)
        # Per IP: dashboard polling is ~9 req/min per open tab; 120/min is
        # ~13 tabs' worth.
        self.ratelimit_dashboard_max: int = _positive_int(
            "CADENCE_RATELIMIT_DASHBOARD_MAX", 120
        )

        # Interactive API docs (/docs, /redoc) and the OpenAPI schema
        # (/openapi.json). Off in production by default: they leak the full
        # route map and need no credential on the backend port. Set
        # CADENCE_API_DOCS_ENABLED=true in a dev .env to turn them on (they
        # then sit behind the same Caddy basic-auth as the read API).
        self.api_docs_enabled: bool = _bool_env("CADENCE_API_DOCS_ENABLED", False)

        # Outbound webhooks. Inert with no webhook rows configured;
        # CADENCE_WEBHOOKS_ENABLED=false is a hard off switch for both the
        # enqueue side (request handlers) and the dispatch side (scheduler).
        self.webhooks_enabled: bool = _bool_env("CADENCE_WEBHOOKS_ENABLED", True)
        # Per-attempt HTTP timeout for a webhook delivery. No outbound call is
        # ever unbounded.
        self.webhook_timeout_seconds: int = _positive_int(
            "CADENCE_WEBHOOK_TIMEOUT_SECONDS", 10
        )
        # Delivery attempts before a webhook_deliveries row is parked in
        # 'failed'. Backoff is min(60s * 2**(n-1), 1h) between attempts.
        self.webhook_max_attempts: int = _positive_int(
            "CADENCE_WEBHOOK_MAX_ATTEMPTS", 6
        )
        # How many pending deliveries the dispatcher drains per scheduler tick.
        self.webhook_dispatch_batch: int = _positive_int(
            "CADENCE_WEBHOOK_DISPATCH_BATCH", 20
        )
        # last_seen_at age past which an active host is considered offline and a
        # host.offline event is enqueued (once, until it reports again).
        # Default matches app.core.staleness.SILENT_AFTER (15 min).
        self.webhook_offline_after_seconds: int = _positive_int(
            "CADENCE_WEBHOOK_OFFLINE_AFTER_SECONDS", 900
        )
        # Terminal (delivered/failed) delivery rows older than this are removed
        # by the scheduler's retention sweep. 0 = keep forever.
        self.webhook_deliveries_retention_days: int = _non_negative_int(
            "CADENCE_WEBHOOK_DELIVERIES_RETENTION_DAYS", 30
        )
        # Cap on the job `log` embedded in job.* payloads (head + tail kept,
        # middle elided). 0 = embed the full stored log.
        self.webhook_log_max_bytes: int = _non_negative_int(
            "CADENCE_WEBHOOK_LOG_MAX_BYTES", 4096
        )

        # Default observation window between campaign stages (roadmap item 5):
        # a stage advances only once all its jobs are terminal AND this many
        # seconds have elapsed with no halt. Overridable per campaign in the
        # create request. 0 = advance as soon as the stage's jobs finish.
        self.campaign_observation_window_seconds: int = _non_negative_int(
            "CADENCE_CAMPAIGN_OBSERVATION_WINDOW_SECONDS", 600
        )


settings = Settings()
