"""widen jobs.job_type to accept health_check

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-11

A new job type: a pure-read job that reruns an apt_upgrade's post-check
phase (dpkg audit, apt dependencies, disk space, failed services, reboot
required) without an upgrade attached, refreshing hosts.health_status on
demand (dashboard button) or once per boot (a dedicated systemd unit
calling POST /api/v1/agent/health-check-job). It reuses the existing
post_checks / health_status result shape, just without pre_checks.

Downgrade restores the narrower CHECK; a surviving health_check row makes
it fail to validate, the same class of issue as narrowing this same CHECK
was in 0016. Acceptable; downgrade does not delete rows.
"""

from __future__ import annotations

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("jobs_job_type_check", "jobs", type_="check")
    op.create_check_constraint(
        "jobs_job_type_check",
        "jobs",
        "job_type IN ('apt_upgrade', 'reboot', 'apt_dry_run', 'health_check')",
    )


def downgrade() -> None:
    op.drop_constraint("jobs_job_type_check", "jobs", type_="check")
    op.create_check_constraint(
        "jobs_job_type_check",
        "jobs",
        "job_type IN ('apt_upgrade', 'reboot', 'apt_dry_run')",
    )
