"""move agent bearer tokens to their own table

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-01

`hosts.token_hash` held exactly one token per host, with no way to rotate,
expire or revoke one without deleting the host. `agent_tokens` holds many
rows per host so a new token can be issued while the old one still works,
then revoked once the agent has switched.

Token state is *derived* -- a row is usable while `revoked_at IS NULL` and
(`expires_at IS NULL OR expires_at > now()`). There is deliberately no
boolean flag that could drift from those timestamps, and no column here
touches `jobs`: revoking a token is an auth-plane change only, it never
cancels a pending or running job.

Auth resolution stays a single ordered chain in get_current_host:
token_hash -> agent_tokens row (else 401) -> not revoked / not expired
(else 401) -> hosts row by host_id -> hosts.is_active (else 401). The host
`is_active` gate is unchanged and still decisive regardless of token state.

The upgrade imports each existing `hosts.token_hash` as one active,
non-expiring row, so agents keep working across the deploy with no
re-provisioning.
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE agent_tokens (
            id           BIGSERIAL PRIMARY KEY,
            host_id      UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
            token_hash   TEXT NOT NULL UNIQUE,
            label        TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_used_at TIMESTAMPTZ,
            expires_at   TIMESTAMPTZ,
            revoked_at   TIMESTAMPTZ
        );
        CREATE INDEX idx_agent_tokens_host ON agent_tokens (host_id);

        INSERT INTO agent_tokens (host_id, token_hash, label, created_at)
        SELECT id, token_hash, 'imported from hosts.token_hash', created_at
        FROM hosts;

        ALTER TABLE hosts DROP COLUMN token_hash;
        """
    )


def downgrade() -> None:
    # Lossy: many tokens collapse to one. A host with no non-revoked token
    # keeps token_hash NULL -- reconcile before relying on the old column.
    op.execute(
        """
        ALTER TABLE hosts ADD COLUMN token_hash TEXT;

        UPDATE hosts h SET token_hash = (
            SELECT t.token_hash FROM agent_tokens t
            WHERE t.host_id = h.id AND t.revoked_at IS NULL
            ORDER BY t.created_at DESC
            LIMIT 1
        );

        DROP TABLE agent_tokens;
        """
    )
