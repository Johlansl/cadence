"""add package_exclusions

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-10

Roadmap item 3, package exclusions / policies. One new table, no change to
any existing one.

An operator-configured rule: never auto-upgrade packages matching `pattern`
(a glob, matched with Python's fnmatch, e.g. "linux-image*"), either
`scope = 'global'` (every host) or `scope = 'host'` (one host, `host_id`
set). The two scopes are additive (a host sees the union of global and its
own rules); there is no re-inclusion mechanism and no tag scope yet
(roadmap item 6). The server resolves patterns to an exact list of package
names against the host's known inventory at job-creation time and sends
that list to the agent in `jobs.params.excluded_packages`; the pattern
itself never reaches the agent.

`scope` gets a CHECK: a small, genuinely closed set (global/host), unlike
`jobs.job_type` or `jobs.failure_category`'s deliberately open ones. A
second CHECK enforces `host_id` is set if and only if `scope = 'host'`, so
a malformed row (global with a stray host_id, or host with none) cannot
exist. `host_id` is nullable with `ON DELETE CASCADE`: the cascade simply
never fires for a NULL (global) row.

No `updated_at`, no `enabled` flag: a rule is created or deleted, not
edited in place.
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE package_exclusions (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            scope       TEXT NOT NULL,
            host_id     UUID REFERENCES hosts(id) ON DELETE CASCADE,
            pattern     TEXT NOT NULL,
            description TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT package_exclusions_scope_check CHECK (scope IN ('global', 'host')),
            CONSTRAINT package_exclusions_scope_host_check CHECK (
                (scope = 'global' AND host_id IS NULL) OR
                (scope = 'host' AND host_id IS NOT NULL)
            )
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS package_exclusions;")
