"""add host_packages.source_package, hosts.os_codename

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-05

Agent 0.7.0 reports each binary package's Debian *source* package name and the
host's release codename (``/etc/os-release`` ``VERSION_CODENAME``). Two nullable
columns hold them so the advisory read-path can match library binaries
(``libssl3`` -> ``openssl``) without the curated fallback map, and resolve the
release for OS versions outside the hardcoded ``VERSION_ID`` list. The columns
are nullable and unbackfilled: pre-0.7.0 reports leave them ``NULL`` and the
read-path keeps using its Phase 1 fallbacks. No existing column changes, so
agent ingestion and every security-count query are untouched.
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE host_packages ADD COLUMN source_package TEXT;
        ALTER TABLE hosts         ADD COLUMN os_codename    TEXT;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE hosts         DROP COLUMN IF EXISTS os_codename;
        ALTER TABLE host_packages DROP COLUMN IF EXISTS source_package;
        """
    )
