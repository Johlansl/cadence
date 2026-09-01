"""add schedules table

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-29

One recurring maintenance window per host (UNIQUE host_id). The `scheduler`
service turns a due row into a `jobs` row. All coherence is enforced in the
database, not only in the API.
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE schedules (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            host_id       UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
            enabled       BOOLEAN NOT NULL DEFAULT true,
            kind          TEXT NOT NULL,
            day_of_month  SMALLINT,
            weekday       SMALLINT,          -- Monday = 0
            hour          SMALLINT NOT NULL,
            minute        SMALLINT NOT NULL DEFAULT 0,
            timezone      TEXT NOT NULL DEFAULT 'UTC',
            params        JSONB NOT NULL DEFAULT '{}'::jsonb,
            last_run_at   TIMESTAMPTZ,
            next_run_at   TIMESTAMPTZ,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT schedules_one_per_host UNIQUE (host_id),
            CONSTRAINT schedules_kind_check CHECK (kind IN ('monthly', 'weekly')),
            CONSTRAINT schedules_hour_range CHECK (hour BETWEEN 0 AND 23),
            CONSTRAINT schedules_minute_range CHECK (minute BETWEEN 0 AND 59),
            CONSTRAINT schedules_kind_fields CHECK (
                (kind = 'monthly' AND day_of_month BETWEEN 1 AND 28 AND weekday IS NULL)
                OR
                (kind = 'weekly' AND weekday BETWEEN 0 AND 6 AND day_of_month IS NULL)
            ),
            CONSTRAINT schedules_reboot_param CHECK (
                NOT (params ? 'reboot') OR params->>'reboot' IN ('auto', 'never')
            )
        );

        CREATE INDEX idx_schedules_due ON schedules (next_run_at) WHERE enabled;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS schedules;")
