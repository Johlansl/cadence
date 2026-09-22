"""Self-monitoring: a Prometheus-text /metrics endpoint for an external scraper.

No dependency (hand-rolled exposition, ~30 lines): `prometheus_client` would
be a new runtime dependency for formatting strings. Read-only aggregates
over small or retention-bounded tables only -- hosts, jobs (terminal rows
pruned after CADENCE_JOBS_RETENTION_DAYS), campaigns, webhook_deliveries
(pruned after CADENCE_WEBHOOK_DELIVERIES_RETENTION_DAYS), scheduler_state.
Never touches reports / raw_payload / host_packages: the CVE-score lesson
(decisions.md "Updates") is that scanning history-scale tables stalls the
process, and a scrape must stay near the cost of /fleet/summary.

On any database error the endpoint still answers 200 with only
`cadence_db_up 0`: Prometheus discards response bodies on non-2xx, so a 503
would hide exactly the signal that matters.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.staleness import LATE_AFTER, SILENT_AFTER
from app.models.models import (
    Campaign,
    Host,
    Job,
    SchedulerState,
    WebhookDelivery,
)
from app.scheduler import HEARTBEAT_STATE_KEY

CONTENT_TYPE = "text/plain; version=0.0.4"


def _quote(value: str) -> str:
    return value.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")


def _gauge(
    out: list[str], name: str, doc: str, rows: list[tuple[dict[str, str], float]]
) -> None:
    out.append(f"# HELP {name} {doc}")
    out.append(f"# TYPE {name} gauge")
    for labels, val in rows:
        if labels:
            inner = ",".join(f'{k}="{_quote(v)}"' for k, v in sorted(labels.items()))
            out.append(f"{name}{{{inner}}} {val}")
        else:
            out.append(f"{name} {val}")


def render_metrics(db: Session, now: datetime | None = None) -> str:
    """Collect every gauge. Raises DBAPIError unchanged on database failure."""
    now = now or datetime.now(timezone.utc)
    out: list[str] = []
    day_ago = now - timedelta(hours=24)

    jobs = db.execute(
        select(Job.status, Job.job_type, func.count())
        .group_by(Job.status, Job.job_type)
    ).all()
    _gauge(
        out,
        "cadence_jobs",
        "Agent jobs by status and type (terminal rows pruned by retention).",
        [({"status": s, "job_type": t}, float(n)) for s, t, n in jobs],
    )
    failed_24h = db.execute(
        select(Job.job_type, func.count())
        .where(Job.status == "failed", Job.completed_at >= day_ago)
        .group_by(Job.job_type)
    ).all()
    _gauge(
        out,
        "cadence_jobs_failed_24h",
        "Jobs failed in the last 24h by type.",
        [({"job_type": t}, float(n)) for t, n in failed_24h],
    )

    campaigns = db.execute(
        select(Campaign.status, func.count()).group_by(Campaign.status)
    ).all()
    _gauge(
        out,
        "cadence_campaigns",
        "Campaigns by status.",
        [({"status": s}, float(n)) for s, n in campaigns],
    )

    pending_dl = db.scalar(
        select(func.count())
        .select_from(WebhookDelivery)
        .where(WebhookDelivery.status == "pending")
    )
    _gauge(
        out,
        "cadence_webhook_deliveries_pending",
        "Webhook deliveries awaiting (re-)dispatch.",
        [({}, float(pending_dl or 0))],
    )
    failed_dl = db.scalar(
        select(func.count())
        .select_from(WebhookDelivery)
        .where(
            WebhookDelivery.status == "failed",
            WebhookDelivery.completed_at >= day_ago,
        )
    )
    _gauge(
        out,
        "cadence_webhook_deliveries_failed_24h",
        "Webhook deliveries parked failed in the last 24h.",
        [({}, float(failed_dl or 0))],
    )

    silent_cut = now - SILENT_AFTER
    late_cut = now - LATE_AFTER
    hosts = db.execute(select(Host.is_active, Host.last_seen_at)).all()
    buckets = {"ok": 0, "late": 0, "silent": 0, "inactive": 0}
    for is_active, last_seen in hosts:
        if not is_active:
            buckets["inactive"] += 1
        elif last_seen is None or last_seen <= silent_cut:
            buckets["silent"] += 1
        elif last_seen <= late_cut:
            buckets["late"] += 1
        else:
            buckets["ok"] += 1
    _gauge(
        out,
        "cadence_hosts",
        "Hosts by freshness bucket (same cutoffs as the dashboard).",
        [({"state": k}, float(v)) for k, v in buckets.items()],
    )
    unhealthy = db.scalar(
        select(func.count())
        .select_from(Host)
        .where(Host.is_active.is_(True), Host.health_status == "unhealthy")
    )
    _gauge(
        out,
        "cadence_hosts_unhealthy",
        "Active hosts whose health projection is unhealthy.",
        [({}, float(unhealthy or 0))],
    )
    versions = db.execute(
        select(Host.agent_version, func.count()).group_by(Host.agent_version)
    ).all()
    _gauge(
        out,
        "cadence_agent_versions",
        "Host rows by reported agent version across the fleet.",
        [({"version": v or "unknown"}, float(n)) for v, n in versions],
    )

    raw = db.scalar(
        select(SchedulerState.value).where(SchedulerState.key == HEARTBEAT_STATE_KEY)
    )
    if raw is not None:
        try:
            age = (now - datetime.fromisoformat(raw)).total_seconds()
        except ValueError:
            age = -1
        _gauge(
            out,
            "cadence_scheduler_heartbeat_age_seconds",
            "Seconds since the scheduler tick heartbeat (-1 if unparseable).",
            [({}, float(age))],
        )

    _gauge(
        out,
        "cadence_db_up",
        "1, the queries above succeeded.",
        [({}, 1.0)],
    )
    return "\n".join(out) + "\n"


def render_down() -> str:
    """Body when the database is unreachable: still 200, only the signal."""
    return (
        "# HELP cadence_db_up 1 if the metrics queries succeeded, else 0.\n"
        "# TYPE cadence_db_up gauge\n"
        "cadence_db_up 0\n"
    )


def collect_metrics(db: Session, now: datetime | None = None) -> str:
    """render_metrics, degraded to render_down() on any database error."""
    try:
        return render_metrics(db, now)
    except DBAPIError:
        return render_down()
