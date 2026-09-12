"""make enrollment_codes survive the deletion of either host it references

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-12

0020 gave enrollment_codes two different behaviors when a host it
references is deleted: target_host_id was ON DELETE CASCADE (the whole
audit row disappears), enrolled_host_id was ON DELETE SET NULL (the row
survives, orphaned). This was an accident, not a design choice, and the
SET NULL side already breaks host deletion outright: deleting any host
enrolled through the "new host" flow hits
enrollment_codes_consumption_check (which required consumed_at NOT NULL to
imply enrolled_host_id NOT NULL), so DELETE /admin/hosts/{id} raises a
Postgres CHECK violation instead of succeeding. Reproduced on a throwaway
database before writing this migration; not something this migration
introduces.

Both columns now go to SET NULL, so deleting a host never destroys an
enrollment_codes row and never blocks the delete: the row becomes an
orphaned but still-readable audit record either way.

0020's two related CHECKs assumed a host reference could never disappear
after consumption, since SET NULL did not exist on target_host_id before
this migration:

- enrollment_codes_target_check required expected_hostname to be set
  whenever target_host_id was NULL. True at creation time, but a consumed,
  host-targeted code whose host is later deleted now has target_host_id
  NULL and expected_hostname still NULL (cleared by the same CHECK's
  creation-time branch, never written again). A third branch allows that
  specific post-consumption orphan state and nothing else; an unconsumed
  code still has to satisfy the original either/or.

- enrollment_codes_consumption_check required consumed_at and
  enrolled_host_id to be NULL or NOT NULL together. The "consumed implies
  still has a host" direction breaks the moment SET NULL fires on an
  already-consumed row. The other, still-useful direction stays: a code
  can never carry an enrolled_host_id without being marked consumed.

No data migration: every row on this database already satisfies both the
old and the new constraints, nothing has been orphaned yet.

Downgrade caveat: restoring the stricter pre-0021 CHECKs will fail if any
enrollment_codes row has been orphaned by a host deletion since this
migration ran (target_host_id or enrolled_host_id now NULL on an already
consumed, previously host-targeted row) -- the same class of one-way
downgrade limit as 0019's job_type CHECK. Reconcile or delete such rows
before downgrading if that has happened; acceptable, downgrade does not
delete rows on its own.
"""

from __future__ import annotations

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE enrollment_codes
            DROP CONSTRAINT enrollment_codes_target_host_id_fkey,
            ADD CONSTRAINT enrollment_codes_target_host_id_fkey
                FOREIGN KEY (target_host_id) REFERENCES hosts(id) ON DELETE SET NULL;

        ALTER TABLE enrollment_codes
            DROP CONSTRAINT enrollment_codes_target_check,
            ADD CONSTRAINT enrollment_codes_target_check CHECK (
               (target_host_id IS NULL AND expected_hostname IS NOT NULL AND btrim(expected_hostname) <> '')
               OR (target_host_id IS NOT NULL AND expected_hostname IS NULL)
               OR (target_host_id IS NULL AND expected_hostname IS NULL AND consumed_at IS NOT NULL)
            );

        ALTER TABLE enrollment_codes
            DROP CONSTRAINT enrollment_codes_consumption_check,
            ADD CONSTRAINT enrollment_codes_consumption_check CHECK (
               enrolled_host_id IS NULL OR consumed_at IS NOT NULL
            );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE enrollment_codes
            DROP CONSTRAINT enrollment_codes_consumption_check,
            ADD CONSTRAINT enrollment_codes_consumption_check CHECK (
               (consumed_at IS NULL AND enrolled_host_id IS NULL)
               OR (consumed_at IS NOT NULL AND enrolled_host_id IS NOT NULL)
            );

        ALTER TABLE enrollment_codes
            DROP CONSTRAINT enrollment_codes_target_check,
            ADD CONSTRAINT enrollment_codes_target_check CHECK (
               (target_host_id IS NULL AND expected_hostname IS NOT NULL AND btrim(expected_hostname) <> '')
               OR (target_host_id IS NOT NULL AND expected_hostname IS NULL)
            );

        ALTER TABLE enrollment_codes
            DROP CONSTRAINT enrollment_codes_target_host_id_fkey,
            ADD CONSTRAINT enrollment_codes_target_host_id_fkey
                FOREIGN KEY (target_host_id) REFERENCES hosts(id) ON DELETE CASCADE;
        """
    )
