"""add hosts.reboot_policy

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-29

Per-host default for what the agent does after an upgrade that needs a reboot.
'never' keeps today's behaviour (the agent never reboots). A per-job override
lives in jobs.params->>'reboot'.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "hosts",
        sa.Column(
            "reboot_policy",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'never'"),
        ),
    )
    op.create_check_constraint(
        "hosts_reboot_policy_check",
        "hosts",
        "reboot_policy IN ('auto', 'never')",
    )


def downgrade() -> None:
    op.drop_constraint("hosts_reboot_policy_check", "hosts", type_="check")
    op.drop_column("hosts", "reboot_policy")
