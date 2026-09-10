"""add package_exclusions.tag and the scope='tag' option

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-10

Roadmap item 6, tag-scoped package exclusions. One new nullable column on
`package_exclusions` and a widening of its two CHECK constraints; no other
table changes.

A rule may now target every host carrying a given tag (`scope = 'tag'`),
alongside `'global'` (every host) and `'host'` (one host). `tag` carries the
same "key" / "key=value" syntax as the `GET /hosts?tag=` filter and campaign
targeting. The three scopes stay additive: a host sees the union of the global
rules, its own host rules, and every tag rule whose tag it carries. No
re-inclusion, no priority.

`scope`'s CHECK widens to (global, host, tag). The coherence CHECK becomes
three-way: for each scope exactly its selector column is set and the other two
are NULL. `btrim(tag) <> ''` is folded in so an empty / whitespace tag (which
the tag matcher would treat as "match every host") can never be stored; the API
rejects it too, this is defence in depth for a raw insert.

Downgrade drops the column and restores the original two-value checks. A
surviving `scope = 'tag'` row makes the restored coherence CHECK fail to
validate, so downgrade errors until such rows are deleted, the same class of
issue as narrowing `jobs_job_type_check` in 0016. Acceptable; downgrade does
not delete rows.
"""

from __future__ import annotations

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE package_exclusions ADD COLUMN tag TEXT;")
    op.drop_constraint(
        "package_exclusions_scope_check", "package_exclusions", type_="check"
    )
    op.drop_constraint(
        "package_exclusions_scope_host_check", "package_exclusions", type_="check"
    )
    op.create_check_constraint(
        "package_exclusions_scope_check",
        "package_exclusions",
        "scope IN ('global', 'host', 'tag')",
    )
    op.create_check_constraint(
        "package_exclusions_scope_host_check",
        "package_exclusions",
        "(scope = 'global' AND host_id IS NULL AND tag IS NULL) OR "
        "(scope = 'host' AND host_id IS NOT NULL AND tag IS NULL) OR "
        "(scope = 'tag' AND tag IS NOT NULL AND btrim(tag) <> '' AND host_id IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "package_exclusions_scope_host_check", "package_exclusions", type_="check"
    )
    op.drop_constraint(
        "package_exclusions_scope_check", "package_exclusions", type_="check"
    )
    op.create_check_constraint(
        "package_exclusions_scope_check",
        "package_exclusions",
        "scope IN ('global', 'host')",
    )
    op.create_check_constraint(
        "package_exclusions_scope_host_check",
        "package_exclusions",
        "(scope = 'global' AND host_id IS NULL) OR "
        "(scope = 'host' AND host_id IS NOT NULL)",
    )
    op.execute("ALTER TABLE package_exclusions DROP COLUMN IF EXISTS tag;")
