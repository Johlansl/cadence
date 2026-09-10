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
  folding that outcome into its counts.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.models import CampaignHost, Job


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
        job = db.get(Job, ch.job_id) if ch.job_id is not None else None
        if job is not None and job.status == "succeeded":
            ch.state = "done"
        elif job is not None and job.status == "failed":
            ch.state = "skipped"
            ch.skip_reason = job.failure_category or "unknown"
        else:
            ch.state = "orphaned"
        ch.updated_at = now
