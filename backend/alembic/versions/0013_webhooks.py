"""add webhooks, webhook_deliveries, webhook_host_state

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-09

Outbound webhook notifications. Three tables, no change to any existing one:

- `webhooks`: an operator-configured endpoint. `secret_encrypted` is a
  Fernet-encrypted copy of the signing secret (same scheme as
  agent_tokens.secret_encrypted, see app/core/crypto.py); the plaintext is
  returned once at creation and never again. `event_types` is a JSON array of
  the event names the endpoint is subscribed to; it is left free (no CHECK)
  the same way jobs.job_type and audit_log.action are, so future event types
  (campaigns) need no migration. The API enforces the closed set with a
  Pydantic Literal.

- `webhook_deliveries`: the outbox. One row is written the moment an event
  occurs, inside the same transaction as the change that produced it, so a
  later HTTP failure never loses it. A dispatcher on the scheduler tick drains
  `status = 'pending'` rows with retry + exponential backoff, moving them to
  'delivered' or (after the attempt cap) 'failed'. `payload` is the exact JSON
  body that gets POSTed. `status` is a closed set and gets a CHECK, like
  jobs.status in 0005.

- `webhook_host_state`: per-host bookkeeping so periodic / repeating conditions
  notify once, not every cycle. `security_updates_notified` is the security
  count at the last notification sent for that host (NULL = never);
  `offline_notified` guards the host.offline event and is cleared when the host
  reports again.
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE webhooks (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            url              TEXT NOT NULL,
            secret_encrypted TEXT NOT NULL,
            enabled          BOOLEAN NOT NULL DEFAULT true,
            event_types      JSONB NOT NULL DEFAULT '[]'::jsonb,
            description      TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE webhook_deliveries (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            webhook_id      UUID NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
            event_type      TEXT NOT NULL,
            payload         JSONB NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            attempt_count   INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_error      TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at    TIMESTAMPTZ
        );

        CREATE INDEX idx_webhook_deliveries_pending
            ON webhook_deliveries (next_attempt_at) WHERE status = 'pending';
        CREATE INDEX idx_webhook_deliveries_webhook
            ON webhook_deliveries (webhook_id);

        CREATE TABLE webhook_host_state (
            host_id                   UUID PRIMARY KEY REFERENCES hosts(id) ON DELETE CASCADE,
            security_updates_notified INTEGER,
            offline_notified          BOOLEAN NOT NULL DEFAULT false,
            updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
    )
    op.create_check_constraint(
        "webhook_deliveries_status_check",
        "webhook_deliveries",
        "status IN ('pending', 'delivered', 'failed')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "webhook_deliveries_status_check", "webhook_deliveries", type_="check"
    )
    op.execute("DROP TABLE IF EXISTS webhook_host_state;")
    op.execute("DROP TABLE IF EXISTS webhook_deliveries;")
    op.execute("DROP TABLE IF EXISTS webhooks CASCADE;")
