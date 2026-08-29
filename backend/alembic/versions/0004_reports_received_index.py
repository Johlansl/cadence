"""index reports.received_at for the retention sweep

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-29

The existing idx_reports_host_received is (host_id, received_at DESC); the
retention DELETE filters on received_at alone across all hosts.
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("idx_reports_received", "reports", ["received_at"])


def downgrade() -> None:
    op.drop_index("idx_reports_received", table_name="reports")
