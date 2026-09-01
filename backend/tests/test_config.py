"""app.core.config.Settings validation."""

from __future__ import annotations

import pytest

from app.core.config import Settings


def test_admin_key_is_required(monkeypatch):
    monkeypatch.delenv("CADENCE_ADMIN_KEY", raising=False)
    with pytest.raises(RuntimeError, match="required"):
        Settings()


def test_admin_key_minimum_length(monkeypatch):
    monkeypatch.setenv("CADENCE_ADMIN_KEY", "too-short")  # 9 chars
    with pytest.raises(RuntimeError, match="at least 12"):
        Settings()


def test_admin_key_previous_is_optional_and_empty_by_default(monkeypatch):
    monkeypatch.setenv("CADENCE_ADMIN_KEY", "a-perfectly-fine-admin-key")
    monkeypatch.delenv("CADENCE_ADMIN_KEY_PREVIOUS", raising=False)
    s = Settings()
    assert s.admin_key == "a-perfectly-fine-admin-key"
    assert s.admin_key_previous == ""


def test_admin_key_previous_is_read_when_set(monkeypatch):
    monkeypatch.setenv("CADENCE_ADMIN_KEY", "a-perfectly-fine-admin-key")
    monkeypatch.setenv("CADENCE_ADMIN_KEY_PREVIOUS", "the-previous-admin-key")
    assert Settings().admin_key_previous == "the-previous-admin-key"
