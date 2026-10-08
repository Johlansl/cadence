"""campaigns may target agent_upgrade, with snapshotted job params

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-07

6B Campaigns C1 (contract + persistence, no engine execution): widen the
campaigns.job_type CHECK to also accept 'agent_upgrade', and add a
campaigns.job_params JSONB snapshot holding the exact params every job of
the campaign will be created with ({"target_version": "N.N.N"} for
agent_upgrade, {} for apt_upgrade). No new table, status or behavior; the
API layer validates the closed params contract, and activation of
agent_upgrade campaigns stays blocked until the engine learns the type.

Downgrade restores the narrower CHECK and drops the column; a surviving
agent_upgrade campaign makes it fail to validate, the same class of issue
as narrowing this same CHECK was in 0016 and 0026. Acceptable; downgrade
does not delete rows and never rewrites agent_upgrade as apt_upgrade.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("campaigns_job_type_check", "campaigns", type_="check")
    op.create_check_constraint(
        "campaigns_job_type_check",
        "campaigns",
        "job_type IN ('apt_upgrade', 'agent_upgrade')",
    )
    op.add_column(
        "campaigns",
        sa.Column(
            "job_params", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "job_params")
    op.drop_constraint("campaigns_job_type_check", "campaigns", type_="check")
    op.create_check_constraint(
        "campaigns_job_type_check",
        "campaigns",
        "job_type IN ('apt_upgrade')",
    )
