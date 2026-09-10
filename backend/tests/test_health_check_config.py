from __future__ import annotations

import pytest

from app.core.config import Settings


def test_upgrade_health_check_defaults(monkeypatch):
    monkeypatch.delenv("CADENCE_UPGRADE_MINIMUM_AVAILABLE_BYTES", raising=False)
    monkeypatch.delenv("CADENCE_UPGRADE_BOOT_MINIMUM_AVAILABLE_BYTES", raising=False)
    monkeypatch.delenv("CADENCE_UPGRADE_LOCK_WAIT_SECONDS", raising=False)
    configured = Settings()

    assert configured.upgrade_minimum_available_bytes == 1024 * 1024 * 1024
    assert configured.upgrade_boot_minimum_available_bytes == 200 * 1024 * 1024
    assert configured.upgrade_lock_wait_seconds == 120


def test_upgrade_health_check_settings_are_configurable(monkeypatch):
    monkeypatch.setenv("CADENCE_UPGRADE_MINIMUM_AVAILABLE_BYTES", "2000000000")
    monkeypatch.setenv("CADENCE_UPGRADE_BOOT_MINIMUM_AVAILABLE_BYTES", "300000000")
    monkeypatch.setenv("CADENCE_UPGRADE_LOCK_WAIT_SECONDS", "240")

    configured = Settings()

    assert configured.upgrade_minimum_available_bytes == 2_000_000_000
    assert configured.upgrade_boot_minimum_available_bytes == 300_000_000
    assert configured.upgrade_lock_wait_seconds == 240


@pytest.mark.parametrize("value", ["0", "-1", "3601", "not-an-integer"])
def test_upgrade_lock_wait_rejects_unsafe_values(monkeypatch, value):
    monkeypatch.setenv("CADENCE_UPGRADE_LOCK_WAIT_SECONDS", value)

    with pytest.raises(RuntimeError, match="CADENCE_UPGRADE_LOCK_WAIT_SECONDS"):
        Settings()
