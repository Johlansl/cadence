from __future__ import annotations

from sqlalchemy import inspect, text


def test_enrollment_schema_and_pending_index(db_session):
    connection = db_session.connection()
    inspector = inspect(connection)

    assert inspector.has_table("enrollment_codes")
    assert inspector.has_table("agent_certificates")

    enrollment_checks = {
        item["name"] for item in inspector.get_check_constraints("enrollment_codes")
    }
    assert {
        "enrollment_codes_ca_fingerprint_check",
        "enrollment_codes_consumption_check",
        "enrollment_codes_expiry_check",
        "enrollment_codes_reboot_policy_check",
        "enrollment_codes_secret_hash_check",
        "enrollment_codes_target_check",
        "enrollment_codes_target_result_check",
        "enrollment_codes_terminal_state_check",
    } <= enrollment_checks

    definition = connection.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema() "
            "AND indexname = 'idx_enrollment_codes_pending_expiry'"
        )
    ).scalar_one()
    assert "WHERE ((consumed_at IS NULL) AND (revoked_at IS NULL))" in definition
