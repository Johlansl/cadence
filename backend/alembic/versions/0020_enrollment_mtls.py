"""add enrollment codes and agent client certificates

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-11

Enrollment codes are short-lived, single-use credentials carrying an
out-of-band server-CA fingerprint. Agent certificates bind the mTLS identity
issued from a consumed code to the same host authenticated by HMAC.
"""

from __future__ import annotations

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE enrollment_codes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            secret_hash TEXT NOT NULL UNIQUE,
            ca_fingerprint_sha256 TEXT NOT NULL,
            target_host_id UUID REFERENCES hosts(id) ON DELETE CASCADE,
            enrolled_host_id UUID REFERENCES hosts(id) ON DELETE SET NULL,
            expected_hostname TEXT,
            label TEXT,
            description TEXT,
            tags JSONB NOT NULL DEFAULT '{}'::jsonb,
            reboot_policy TEXT NOT NULL DEFAULT 'never',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            consumed_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            CONSTRAINT enrollment_codes_secret_hash_check CHECK (secret_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT enrollment_codes_ca_fingerprint_check CHECK (ca_fingerprint_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT enrollment_codes_expiry_check CHECK (expires_at > created_at),
            CONSTRAINT enrollment_codes_reboot_policy_check CHECK (reboot_policy IN ('auto','never','prompt')),
            CONSTRAINT enrollment_codes_target_check CHECK (
               (target_host_id IS NULL AND expected_hostname IS NOT NULL AND btrim(expected_hostname) <> '')
               OR (target_host_id IS NOT NULL AND expected_hostname IS NULL)
            ),
            CONSTRAINT enrollment_codes_consumption_check CHECK (
               (consumed_at IS NULL AND enrolled_host_id IS NULL)
               OR (consumed_at IS NOT NULL AND enrolled_host_id IS NOT NULL)
            ),
            CONSTRAINT enrollment_codes_terminal_state_check CHECK (NOT (consumed_at IS NOT NULL AND revoked_at IS NOT NULL)),
            CONSTRAINT enrollment_codes_target_result_check CHECK (
               target_host_id IS NULL OR enrolled_host_id IS NULL OR target_host_id = enrolled_host_id
            )
        );
        CREATE INDEX idx_enrollment_codes_target_host ON enrollment_codes(target_host_id);
        CREATE INDEX idx_enrollment_codes_pending_expiry ON enrollment_codes(expires_at)
         WHERE consumed_at IS NULL AND revoked_at IS NULL;
        CREATE TABLE agent_certificates (
            id BIGSERIAL PRIMARY KEY,
            host_id UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
            enrollment_code_id UUID REFERENCES enrollment_codes(id) ON DELETE SET NULL,
            serial_number TEXT NOT NULL UNIQUE,
            fingerprint_sha256 TEXT NOT NULL UNIQUE,
            not_before TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_used_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            CONSTRAINT agent_certificates_serial_check CHECK (serial_number ~ '^[0-9a-f]+$'),
            CONSTRAINT agent_certificates_fingerprint_check CHECK (fingerprint_sha256 ~ '^[0-9a-f]{64}$'),
            CONSTRAINT agent_certificates_expiry_check CHECK (expires_at > not_before)
        );
        CREATE INDEX idx_agent_certificates_host ON agent_certificates(host_id);
        CREATE INDEX idx_agent_certificates_host_expiry ON agent_certificates(host_id, expires_at);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE agent_certificates;
        DROP TABLE enrollment_codes;
        """
    )
