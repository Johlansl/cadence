"""add campaigns, campaign_hosts, jobs.campaign_id, jobs.job_type CHECK

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-10

Roadmap item 5, campaigns. A campaign is a staged, rate-limited rollout of
`apt_upgrade` jobs across a fixed set of hosts, driven by a new
`advance_campaigns()` pass on the scheduler tick. It creates ordinary
`apt_upgrade` jobs (no new job_type); the agent never sees the campaign.

Two new tables, one new nullable column on `jobs`, no change to any existing
column:

- `campaigns`: one rollout. `stages` is an ordered JSON array of wave sizes
  (an int = absolute host count, "N%" = percent of the resolved total, "rest"
  = all remaining and only valid last); the targeted hosts are sliced into
  `campaign_hosts` rows once, at creation, and never re-resolved. `status`
  gets a CHECK the same way `jobs.status` does (0005): a closed set the engine
  and the action routes both rely on. `job_type` is fixed to 'apt_upgrade' in
  v1 (CHECK), the column is kept so a later kind can be added without a table
  change. `halt_reason` is set only when `status='stopped'`. The current stage
  index and the stage-ready timestamp are recomputed at read time from
  `campaign_hosts` / `jobs`, not stored (same "no snapshot column" choice as
  `excluded_count` etc.).

- `campaign_hosts`: a host's membership, its frozen `stage_index`, and the
  engine's per-host bookkeeping. `state`: pending -> running -> done | skipped,
  plus 'orphaned' (terminal) for a host whose job was still in flight, or not
  yet created, when the campaign was halted / cancelled -- the job runs to
  completion on the agent and its result is still recorded in `jobs`, the
  campaign just stops folding it into its counts. `skip_reason` carries the
  job's `failure_category` when `state='skipped'`. Both `state` and the two
  scope-style invariants get CHECKs.

- `jobs.campaign_id`: set for a job the campaign engine created for one of its
  hosts, NULL for every manually / scheduler-created job (the unchanged path).
  `ON DELETE SET NULL` so a campaign row could be removed later without losing
  job history; a partial index supports the engine's in-flight count.

- `jobs.job_type` CHECK: deferred since 0005 ("a dedicated 'reboot' type is
  planned"). Campaigns add no job_type, so this closes it now on the three
  values the API `Literal` has always enforced -- `apt_upgrade`, `reboot`,
  `apt_dry_run`. Every existing row is one of these (the Literal is the only
  writer), so the constraint validates clean. Widening it later (a future
  job_type) is a drop + recreate, like `jobs.status` would be.
"""

from __future__ import annotations

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE campaigns (
            id                         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name                       TEXT NOT NULL,
            job_type                   TEXT NOT NULL DEFAULT 'apt_upgrade',
            stages                     JSONB NOT NULL,
            max_concurrency            INTEGER NOT NULL,
            max_failures               INTEGER NOT NULL,
            observation_window_seconds INTEGER NOT NULL,
            status                     TEXT NOT NULL DEFAULT 'draft',
            halt_reason                TEXT,
            requested_by               TEXT,
            created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
            started_at                 TIMESTAMPTZ,
            completed_at               TIMESTAMPTZ,
            CONSTRAINT campaigns_status_check CHECK (
                status IN ('draft', 'running', 'paused', 'completed', 'stopped', 'cancelled')
            ),
            CONSTRAINT campaigns_job_type_check CHECK (job_type IN ('apt_upgrade')),
            CONSTRAINT campaigns_concurrency_check CHECK (max_concurrency >= 1),
            CONSTRAINT campaigns_failures_check CHECK (max_failures >= 0),
            CONSTRAINT campaigns_window_check CHECK (observation_window_seconds >= 0)
        );

        CREATE TABLE campaign_hosts (
            campaign_id UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
            host_id     UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
            stage_index SMALLINT NOT NULL,
            state       TEXT NOT NULL DEFAULT 'pending',
            skip_reason TEXT,
            job_id      UUID REFERENCES jobs(id) ON DELETE SET NULL,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (campaign_id, host_id),
            CONSTRAINT campaign_hosts_state_check CHECK (
                state IN ('pending', 'running', 'done', 'skipped', 'orphaned')
            ),
            CONSTRAINT campaign_hosts_stage_check CHECK (stage_index >= 0),
            CONSTRAINT campaign_hosts_skip_reason_check CHECK (
                (state = 'skipped') = (skip_reason IS NOT NULL)
            )
        );

        CREATE INDEX idx_campaign_hosts_campaign ON campaign_hosts (campaign_id);

        ALTER TABLE jobs
            ADD COLUMN campaign_id UUID REFERENCES campaigns(id) ON DELETE SET NULL;

        CREATE INDEX idx_jobs_campaign ON jobs (campaign_id) WHERE campaign_id IS NOT NULL;
        """
    )
    op.create_check_constraint(
        "jobs_job_type_check",
        "jobs",
        "job_type IN ('apt_upgrade', 'reboot', 'apt_dry_run')",
    )


def downgrade() -> None:
    op.drop_constraint("jobs_job_type_check", "jobs", type_="check")
    op.execute(
        """
        ALTER TABLE jobs DROP COLUMN IF EXISTS campaign_id;
        DROP TABLE IF EXISTS campaign_hosts;
        DROP TABLE IF EXISTS campaigns CASCADE;
        """
    )
