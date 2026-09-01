"""baseline -- the V1 schema as first shipped via init.sql

Revision ID: 0001
Revises:
Create Date: 2026-08-29

This mirrors backend/app/db/init.sql exactly. On a database that already had
init.sql applied (the normal case), this revision is never executed -- it is
recorded with `alembic stamp 0001`. It only runs when a database is built from
Alembic alone (e.g. the test suite).
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


SCHEMA = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE hosts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hostname            TEXT NOT NULL,
    fqdn                TEXT,
    description         TEXT,
    token_hash          TEXT NOT NULL UNIQUE,
    os_family           TEXT NOT NULL DEFAULT 'debian',
    os_name             TEXT,
    os_version          TEXT,
    package_manager     TEXT NOT NULL DEFAULT 'apt',
    agent_version       TEXT,
    tags                JSONB NOT NULL DEFAULT '{}'::jsonb,
    reboot_required     BOOLEAN NOT NULL DEFAULT false,
    is_active           BOOLEAN NOT NULL DEFAULT true,
    last_seen_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE packages (
    id           BIGSERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    architecture TEXT NOT NULL DEFAULT '',
    UNIQUE (name, architecture)
);

CREATE TABLE host_packages (
    host_id             UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    package_id          BIGINT NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    installed_version   TEXT NOT NULL,
    candidate_version   TEXT,
    is_security_update  BOOLEAN NOT NULL DEFAULT false,
    update_origin       TEXT,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (host_id, package_id)
);

CREATE INDEX idx_host_packages_pending_updates
    ON host_packages (host_id) WHERE candidate_version IS NOT NULL;

CREATE TABLE reports (
    id                       BIGSERIAL PRIMARY KEY,
    host_id                  UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    received_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent_version            TEXT,
    installed_package_count  INTEGER NOT NULL DEFAULT 0,
    updates_available_count  INTEGER NOT NULL DEFAULT 0,
    security_updates_count   INTEGER NOT NULL DEFAULT 0,
    reboot_required          BOOLEAN NOT NULL DEFAULT false,
    raw_payload              JSONB NOT NULL
);

CREATE INDEX idx_reports_host_received ON reports (host_id, received_at DESC);

CREATE TABLE jobs (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    host_id        UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    job_type       TEXT NOT NULL DEFAULT 'apt_upgrade',
    status         TEXT NOT NULL DEFAULT 'pending',
    params         JSONB NOT NULL DEFAULT '{}'::jsonb,
    requested_by   TEXT,
    result         JSONB,
    log            TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at     TIMESTAMPTZ,
    completed_at   TIMESTAMPTZ
);

CREATE INDEX idx_jobs_host_status ON jobs (host_id, status);
CREATE INDEX idx_jobs_pending ON jobs (host_id) WHERE status = 'pending';
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS jobs CASCADE;
        DROP TABLE IF EXISTS reports CASCADE;
        DROP TABLE IF EXISTS host_packages CASCADE;
        DROP TABLE IF EXISTS packages CASCADE;
        DROP TABLE IF EXISTS hosts CASCADE;
        """
    )
