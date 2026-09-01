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


def test_trusted_proxies_empty_by_default(monkeypatch):
    monkeypatch.setenv("CADENCE_ADMIN_KEY", "a-perfectly-fine-admin-key")
    monkeypatch.delenv("CADENCE_TRUSTED_PROXIES", raising=False)
    assert Settings().trusted_proxies == []


def test_trusted_proxies_parses_a_mixed_list(monkeypatch):
    monkeypatch.setenv("CADENCE_ADMIN_KEY", "a-perfectly-fine-admin-key")
    monkeypatch.setenv("CADENCE_TRUSTED_PROXIES", "172.20.0.0/16, 10.1.2.3 192.168.1.0/24")
    nets = Settings().trusted_proxies
    assert [str(n) for n in nets] == ["172.20.0.0/16", "10.1.2.3/32", "192.168.1.0/24"]


def test_trusted_proxies_rejects_garbage(monkeypatch):
    monkeypatch.setenv("CADENCE_ADMIN_KEY", "a-perfectly-fine-admin-key")
    monkeypatch.setenv("CADENCE_TRUSTED_PROXIES", "not-an-ip")
    with pytest.raises(RuntimeError, match="not a valid CIDR"):
        Settings()
