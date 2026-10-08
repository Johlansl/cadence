"""widen jobs.job_type to accept agent_upgrade

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-30

A new declarative job type (6B): the server names an agent target version
in jobs.params ({"target_version": "N.N.N"}) and the agent, once it learns
the type, upgrades itself to it. This migration only widens the CHECK; no
new table, column or status. The API layer validates the closed params
contract (schemas.JobCreate); the DB stays a plain closed set like the
other job types.

No agent executes agent_upgrade yet: current agents refuse it as an
unsupported job type (agent_refused), the same path as any unknown type.

Downgrade restores the narrower CHECK; a surviving agent_upgrade row makes
it fail to validate, the same class of issue as narrowing this same CHECK
was in 0016 and 0019. Acceptable; downgrade does not delete rows.
"""

from __future__ import annotations

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("jobs_job_type_check", "jobs", type_="check")
    op.create_check_constraint(
        "jobs_job_type_check",
        "jobs",
        "job_type IN ('apt_upgrade', 'reboot', 'apt_dry_run', 'health_check', 'agent_upgrade')",
    )


def downgrade() -> None:
    op.drop_constraint("jobs_job_type_check", "jobs", type_="check")
    op.create_check_constraint(
        "jobs_job_type_check",
        "jobs",
        "job_type IN ('apt_upgrade', 'reboot', 'apt_dry_run', 'health_check')",
    )
