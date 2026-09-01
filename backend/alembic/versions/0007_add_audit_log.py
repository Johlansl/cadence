"""add audit_log

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-01

An append-only trail of successful admin writes -- the eight X-Admin-Key
endpoints that mutate state (create / patch / delete a host, queue or clear
jobs, create / update / delete a schedule). The row is written in the same
transaction as the mutation, so only a change that actually committed leaves
a trace and a handler that raised leaves none.

No foreign key on target_id: the row must outlive the host / schedule / job
it refers to -- a delete is exactly the kind of action worth keeping. `action`
is free text ("resource.verb"), an open set with no CHECK, so a new admin
endpoint just picks a new verb.
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE audit_log (
            id          BIGSERIAL PRIMARY KEY,
            at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            action      TEXT NOT NULL,
            target_type TEXT,
            target_id   TEXT,
            actor       TEXT NOT NULL DEFAULT 'admin',
            client      TEXT,
            request_id  TEXT,
            detail      JSONB
        );
        CREATE INDEX idx_audit_at     ON audit_log (at DESC);
        CREATE INDEX idx_audit_target ON audit_log (target_type, target_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_log;")
