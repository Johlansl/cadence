"""hold the pre-reboot boot reference on campaign rows

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-28

6A reboot orchestration, server-side expand step (no behavior change yet).
`campaign_hosts.awaited_boot` will hold the pre-reboot boot identifier the
engine snapshots when a campaign upgrade decides to reboot; non-NULL marks a
row as awaiting proven return. Nothing writes or reads it until lot 3.

Storing the reference on the campaign row (rather than reading it back from
the upgrade job's result) keeps the proof independent of the job row: deleting
job history cannot strand a row in an undecidable state.
"""

from __future__ import annotations

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE campaign_hosts ADD COLUMN awaited_boot TEXT;")


def downgrade() -> None:
    op.execute("ALTER TABLE campaign_hosts DROP COLUMN IF EXISTS awaited_boot;")
