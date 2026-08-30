"""allow reboot_policy / params.reboot = 'prompt'

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-30

'prompt' is a third reboot mode: the agent never reboots on its own (like
'never'), the dashboard surfaces "reboot required", and the operator triggers
a reboot when ready via a dedicated job_type='reboot' job. job_type stays
free text, so no constraint there.
"""

from __future__ import annotations

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("hosts_reboot_policy_check", "hosts", type_="check")
    op.create_check_constraint(
        "hosts_reboot_policy_check",
        "hosts",
        "reboot_policy IN ('auto', 'never', 'prompt')",
    )
    op.drop_constraint("schedules_reboot_param", "schedules", type_="check")
    op.create_check_constraint(
        "schedules_reboot_param",
        "schedules",
        "NOT (params ? 'reboot') OR params->>'reboot' IN ('auto', 'never', 'prompt')",
    )


def downgrade() -> None:
    # Fails if any row still uses 'prompt' -- reconcile first.
    op.drop_constraint("hosts_reboot_policy_check", "hosts", type_="check")
    op.create_check_constraint(
        "hosts_reboot_policy_check",
        "hosts",
        "reboot_policy IN ('auto', 'never')",
    )
    op.drop_constraint("schedules_reboot_param", "schedules", type_="check")
    op.create_check_constraint(
        "schedules_reboot_param",
        "schedules",
        "NOT (params ? 'reboot') OR params->>'reboot' IN ('auto', 'never')",
    )
