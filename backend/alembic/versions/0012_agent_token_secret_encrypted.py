"""add agent_tokens.secret_encrypted

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-07

Phase 1 of the agent auth hardening (docs/decisions.md "Authentication"):
signed requests need the server to recover the real secret to verify an HMAC,
not just a one-way hash. This column holds a Fernet-encrypted copy of the
token, written only at issuance time from now on (CADENCE_TOKEN_ENCRYPTION_KEY,
see app/core/crypto.py). Nullable and unbackfilled on purpose: the plaintext
of every token issued before this revision was never stored anywhere and
cannot be recovered, so those rows stay NULL and can only ever authenticate
via the existing bearer path until they are rotated. No existing column
changes, so the legacy bearer path is untouched.
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_tokens ADD COLUMN secret_encrypted TEXT;")


def downgrade() -> None:
    op.execute("ALTER TABLE agent_tokens DROP COLUMN IF EXISTS secret_encrypted;")
