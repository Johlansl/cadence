"""one active job per host, enforced by PostgreSQL

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-14

Audit P0 (R1): the "one active job per host" rule lived only in
app.job_creation.create_job_for_host as a SELECT-then-INSERT check, so two
concurrent creators could both pass the check and insert. This revision
makes PostgreSQL the final authority with a partial unique index on
jobs(host_id) covering only the active statuses. Terminal rows
(succeeded/failed) stay unlimited per host.

No destructive repair: if an old race already left several active jobs on
one host, the migration refuses with a diagnostic listing the offenders.
Resolve manually (let the jobs finish, or DELETE
/api/v1/admin/hosts/{id}/jobs), then re-run. Downgrade drops the index and
touches no data.
"""

from __future__ import annotations

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            dup_count integer;
            dup_sample text;
        BEGIN
            SELECT count(*) INTO dup_count FROM (
                SELECT host_id
                FROM jobs
                WHERE status IN ('pending', 'running')
                GROUP BY host_id
                HAVING count(*) > 1
            ) dups;
            IF dup_count > 0 THEN
                SELECT string_agg(
                    format('host %s: jobs %s', host_id, job_ids), '; '
                ) INTO dup_sample FROM (
                    SELECT host_id, array_agg(id ORDER BY created_at) AS job_ids
                    FROM jobs
                    WHERE status IN ('pending', 'running')
                    GROUP BY host_id
                    HAVING count(*) > 1
                    LIMIT 20
                ) d;
                RAISE EXCEPTION
                    'migration 0023 refused: % host(s) have several active jobs: %. '
                    'Resolve manually (let the jobs finish or DELETE '
                    '/api/v1/admin/hosts/{id}/jobs), then re-run.',
                    dup_count, dup_sample;
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_jobs_one_active_per_host
            ON jobs (host_id)
            WHERE status IN ('pending', 'running');
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_jobs_one_active_per_host;")
