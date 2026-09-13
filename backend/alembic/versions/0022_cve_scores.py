"""add cve_scores cache table

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-13

Roadmap item 9, CVE/CVSS severity. The DSA/DLA pipeline stays the source of
truth for which CVEs concern which package; this table only caches the CVSS
score for one CVE so the read API never fetches from the network:

- `base_score` / `base_severity`: the NVD CVSS base score (0.0-10.0, one
  decimal) and severity (NONE / LOW / MEDIUM / HIGH / CRITICAL) selected by
  the refresh step. Both NULL mean "unknown": the NVD has no metrics for
  this CVE, or it has not been fetched yet. NULL is never a zero and never
  evidence that a host is unaffected.
- `vector` / `cvss_version`: the CVSS vector string and version the score
  was taken from (e.g. "3.1"), for auditability.
- `source`: where the score came from ('nvd' today); a fixed default, not
  a CHECK, so a future source needs no migration.
- `fetched_at`: last successful refresh of this row, for ops visibility.

One row per CVE, upserted by the scheduler's CVE-score refresh; rows for
CVEs no longer referenced are left in place (same policy as advisories in
0010: a CVE record is not retracted, and GC is a later concern). No
existing table changes.
"""

from __future__ import annotations

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE cve_scores (
            cve_id        TEXT PRIMARY KEY,
            base_score    NUMERIC(3, 1),
            base_severity TEXT,
            vector        TEXT,
            cvss_version  TEXT,
            source        TEXT NOT NULL DEFAULT 'nvd',
            fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX idx_cve_scores_fetched_at
            ON cve_scores (fetched_at);
        """
    )
    op.create_check_constraint(
        "cve_scores_severity_check",
        "cve_scores",
        "base_severity IN ('NONE', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL')",
    )
    op.create_check_constraint(
        "cve_scores_score_range_check",
        "cve_scores",
        "base_score BETWEEN 0 AND 10",
    )


def downgrade() -> None:
    op.drop_constraint("cve_scores_score_range_check", "cve_scores", type_="check")
    op.drop_constraint("cve_scores_severity_check", "cve_scores", type_="check")
    op.execute("DROP TABLE IF EXISTS cve_scores;")
