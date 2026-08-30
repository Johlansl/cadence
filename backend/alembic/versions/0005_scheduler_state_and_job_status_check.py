"""add scheduler_state, constrain jobs.status

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-30

scheduler_state is a tiny key/value table so the daily retention sweep
remembers when it last ran across scheduler restarts (previously an
in-process variable, so every redeploy re-ran the sweep).

jobs.status was free text; the values in use are a closed set. A CHECK
keeps a typo or a stale writer from parking a job in an unknown state
that the reaper / pickup logic would ignore. job_type stays free text
(a dedicated 'reboot' type is planned).
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE scheduler_state (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.create_check_constraint(
        "jobs_status_check",
        "jobs",
        "status IN ('pending', 'running', 'succeeded', 'failed')",
    )


def downgrade() -> None:
    op.drop_constraint("jobs_status_check", "jobs", type_="check")
    op.execute("DROP TABLE IF EXISTS scheduler_state;")
