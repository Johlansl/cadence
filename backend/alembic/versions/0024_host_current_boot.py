"""track the host's current boot

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-28

6A reboot orchestration, server-side expand step (no behavior change yet).
`hosts.current_boot_id` holds the boot identifier most recently observed from
the agent (reports, job results, polls), NULL until a capable agent makes
contact. The engine (lot 3) compares it against the pre-reboot reference to
prove a host came back; this revision only stores the observation.

Existing hosts start at NULL. No backfill: past contacts carried no boot
identifier, so there is nothing trustworthy to derive it from.
"""

from __future__ import annotations

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE hosts ADD COLUMN current_boot_id TEXT;")


def downgrade() -> None:
    op.execute("ALTER TABLE hosts DROP COLUMN IF EXISTS current_boot_id;")
