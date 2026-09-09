"""add jobs.failure_category and jobs.failure_summary

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-09

Roadmap item 2, job-failure classification. A failed job now carries a coarse
category and a one-line summary instead of only the raw log blob.

- `failure_category`: one of an open set the code knows -- `apt_locked`,
  `network_or_repo`, `dpkg_error`, `disk_full`, `timeout`, `agent_lost`,
  `agent_refused`, `unknown`. The agent classifies everything derived from a
  real run; the scheduler reaper classifies a job that was reaped with no
  result (`timeout` vs `agent_lost`). No CHECK, for the same reason
  `jobs.status` and `jobs.job_type` have none (migration 0005): campaigns will
  add categories, and widening a CHECK later is a drop+recreate. The API does
  not constrain the value either -- it is agent-authoritative.
- `failure_summary`: a single line lifted from the apt/dpkg output (or a
  synthetic line for the no-output cases), for the dashboard and the
  `job.failed` webhook so a consumer need not parse the truncated log.

Both are nullable and only meaningful when `status = 'failed'`. Not backfilled:
jobs that failed before this revision stay NULL ("uncategorised" in the UI).
No existing column changes.
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE jobs ADD COLUMN failure_category TEXT;")
    op.execute("ALTER TABLE jobs ADD COLUMN failure_summary TEXT;")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS failure_summary;")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS failure_category;")
