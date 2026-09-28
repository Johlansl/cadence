"""Terminal-state bookkeeping for campaigns.

`finalize_campaign_hosts` runs in the same transaction as a campaign moving to
'stopped' (the engine, app.campaigns.engine) or 'cancelled' (the admin route).
After that transition the engine never looks at the campaign again, so no
campaign_hosts row may be left non-terminal:

- a host whose job already reached a terminal status is reconciled to its real
  outcome (done / skipped), with no disposition side effects (the campaign is
  already terminal);
- a host whose job is still pending / running, or that never got a job, becomes
  'orphaned' -- the job (if any) runs to completion on the agent and its result
  is still recorded in `jobs` by the normal callback, the campaign just stops
  folding that outcome into its counts;
- a host still awaiting proven return (awaited_boot set) becomes 'orphaned':
  its upgrade finished but the return was never proven, so 'done' would lie.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.models import CampaignHost, Host, Job


def reboot_expected(job: Job, host: Host | None, result: dict) -> bool:
    """True when the upgrade ran on auto with a pending reboot: the agent
    rebooted (old agents decide the same way) but sent no proof fields. The
    mode is the creation-time snapshot when present, else the live policy
    (rows created before pinning, same fallback the claim path uses)."""
    mode = (job.params or {}).get("reboot") or (host.reboot_policy if host else None)
    return mode == "auto" and bool(result.get("reboot_required"))


def finalize_campaign_hosts(
    db: Session, campaign_id: uuid.UUID, *, now: datetime
) -> None:
    """Move every still-in-flight campaign_hosts row to a terminal state. Does
    not commit; the caller owns the transaction."""
    rows = (
        db.execute(
            select(CampaignHost).where(
                CampaignHost.campaign_id == campaign_id,
                CampaignHost.state.in_(("pending", "running")),
            )
        )
        .scalars()
        .all()
    )
    for ch in rows:
        if ch.awaited_boot is not None:
            # Return was never proven (a proven row would already be done):
            # orphan, never done.
            ch.state = "orphaned"
            ch.updated_at = now
            continue
        job = db.get(Job, ch.job_id) if ch.job_id is not None else None
        if job is not None and job.status == "succeeded":
            result = job.result or {}
            will_reboot = result.get("will_reboot")
            unproven = will_reboot is True or (
                will_reboot is None
                and reboot_expected(job, db.get(Host, ch.host_id), result)
            )
            # The submit -> reconcile window: the job succeeded with a reboot
            # the engine never saw, so there is no await marker yet. Same rule
            # as an awaited row: the return is unproven, orphan, never done.
            ch.state = "orphaned" if unproven else "done"
        elif job is not None and job.status == "failed":
            ch.state = "skipped"
            ch.skip_reason = job.failure_category or "unknown"
        else:
            ch.state = "orphaned"
        ch.updated_at = now
