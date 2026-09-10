"""add the current health projection to hosts

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-10

Roadmap item 7, pre- and post-upgrade health checks. The structured evidence
stays on the originating job in jobs.result; hosts only carries the current
projection needed by fleet and host views:

- `health_status`: `healthy`, `degraded`, `unhealthy`, or `unknown`;
- `health_checked_at`: completion time of the apt_upgrade job that produced
  that status, NULL until an agent capable of health checks submits a result.

Existing and newly provisioned hosts start at `unknown`. No historical job is
backfilled because older results do not contain the post-check evidence needed
to derive a trustworthy status.
"""

from __future__ import annotations

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE hosts ADD COLUMN health_status TEXT NOT NULL DEFAULT 'unknown';"
    )
    op.execute("ALTER TABLE hosts ADD COLUMN health_checked_at TIMESTAMPTZ;")
    op.create_check_constraint(
        "hosts_health_status_check",
        "hosts",
        "health_status IN ('healthy', 'degraded', 'unhealthy', 'unknown')",
    )


def downgrade() -> None:
    op.drop_constraint("hosts_health_status_check", "hosts", type_="check")
    op.execute("ALTER TABLE hosts DROP COLUMN IF EXISTS health_checked_at;")
    op.execute("ALTER TABLE hosts DROP COLUMN IF EXISTS health_status;")
