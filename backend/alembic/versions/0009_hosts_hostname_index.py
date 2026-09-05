"""index hosts (hostname, id) for keyset pagination

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-05

GET /api/v1/hosts gained keyset pagination ordered by (hostname, id) -- the
id tiebreaker keeps rows that share a hostname from being split across a page
boundary. This composite index backs that scan; there was no index on
hosts.hostname before (only the primary key on id).
"""

from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("idx_hosts_hostname_id", "hosts", ["hostname", "id"])


def downgrade() -> None:
    op.drop_index("idx_hosts_hostname_id", table_name="hosts")
