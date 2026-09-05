"""add advisories, advisory_packages

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-05

Two small tables holding upstream security advisories (Debian DSA/DLA now,
Ubuntu USN later) and the per-release fixed-version rows the read API joins a
host's pending security updates against. They are written only by the
scheduler's advisory-refresh task, never on the agent report path, and no
existing table changes -- so agent ingestion and every security-count query
are untouched. `advisory_packages.package` is the Debian *source* package
name; the read side maps binary names onto it.
"""

from __future__ import annotations

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE advisories (
            id           TEXT PRIMARY KEY,
            source       TEXT NOT NULL,
            url          TEXT NOT NULL,
            title        TEXT,
            cve_ids      JSONB NOT NULL DEFAULT '[]'::jsonb,
            published_at TIMESTAMPTZ,
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE advisory_packages (
            advisory_id   TEXT NOT NULL REFERENCES advisories(id) ON DELETE CASCADE,
            release       TEXT NOT NULL,
            package       TEXT NOT NULL,
            fixed_version TEXT NOT NULL,
            PRIMARY KEY (advisory_id, release, package)
        );

        CREATE INDEX idx_advisory_packages_lookup
            ON advisory_packages (package, release);
        """
    )
    op.create_check_constraint(
        "advisories_source_check",
        "advisories",
        "source IN ('debian-dsa', 'debian-dla', 'ubuntu-usn')",
    )


def downgrade() -> None:
    op.drop_constraint("advisories_source_check", "advisories", type_="check")
    op.execute("DROP TABLE IF EXISTS advisory_packages;")
    op.execute("DROP TABLE IF EXISTS advisories;")
