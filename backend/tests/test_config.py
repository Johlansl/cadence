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


def _valid_env(monkeypatch):
    monkeypatch.setenv("CADENCE_ADMIN_KEY", "a-perfectly-fine-admin-key")


def test_ratelimit_defaults(monkeypatch):
    _valid_env(monkeypatch)
    for var in (
        "CADENCE_RATELIMIT_ENABLED",
        "CADENCE_RATELIMIT_WINDOW_SECONDS",
        "CADENCE_RATELIMIT_AGENT_MAX",
        "CADENCE_RATELIMIT_ADMIN_MAX",
        "CADENCE_RATELIMIT_DASHBOARD_MAX",
    ):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert s.ratelimit_enabled is True
    assert s.ratelimit_window_seconds == 60
    assert (s.ratelimit_agent_max, s.ratelimit_admin_max, s.ratelimit_dashboard_max) == (
        20,
        60,
        120,
    )


def test_ratelimit_can_be_disabled(monkeypatch):
    _valid_env(monkeypatch)
    monkeypatch.setenv("CADENCE_RATELIMIT_ENABLED", "false")
    assert Settings().ratelimit_enabled is False


@pytest.mark.parametrize("bad", ["abc", "0", "-1"])
def test_ratelimit_max_rejects_non_positive(monkeypatch, bad):
    _valid_env(monkeypatch)
    monkeypatch.setenv("CADENCE_RATELIMIT_AGENT_MAX", bad)
    with pytest.raises(RuntimeError):
        Settings()


def test_api_docs_disabled_by_default(monkeypatch):
    _valid_env(monkeypatch)
    monkeypatch.delenv("CADENCE_API_DOCS_ENABLED", raising=False)
    assert Settings().api_docs_enabled is False
    monkeypatch.setenv("CADENCE_API_DOCS_ENABLED", "true")
    assert Settings().api_docs_enabled is True
