"""app.core.ratelimit: a volume cap on *successful* authenticated traffic,
separate from the auth-failure throttle (test_auth_throttle.py)."""

from __future__ import annotations

import logging

import pytest

from app.core import ratelimit as ratelimit_mod
from app.core.config import settings
from app.core.ratelimit import RateLimiter, ratelimiter
from tests.conftest import ADMIN_HEADERS, create_host, report_payload, signed

# --- unit: RateLimiter.check --------------------------------------------------


def test_within_the_cap_is_allowed_then_429_owed():
    rl = RateLimiter(enabled=True)
    assert [rl.check("k", limit=3, window=60) for _ in range(3)] == [0.0, 0.0, 0.0]
    owed = rl.check("k", limit=3, window=60)
    assert 0 < owed <= 60


def test_retry_after_never_exceeds_the_window():
    rl = RateLimiter(enabled=True)
    for _ in range(10):
        rl.check("k", limit=1, window=5)
    assert 0 < rl.check("k", limit=1, window=5) <= 5


def test_keys_are_independent():
    rl = RateLimiter(enabled=True)
    for _ in range(5):
        rl.check("a", limit=2, window=60)
    assert rl.check("a", limit=2, window=60) > 0  # a is capped
    assert rl.check("b", limit=2, window=60) == 0.0  # b has its own bucket


def test_window_rolls_over(monkeypatch):
    rl = RateLimiter(enabled=True)
    clock = [1000.0]
    monkeypatch.setattr(ratelimit_mod.time, "monotonic", lambda: clock[0])

    for _ in range(3):
        rl.check("k", limit=3, window=60)
    assert rl.check("k", limit=3, window=60) > 0  # capped inside the window

    clock[0] += 61  # window has rolled
    assert rl.check("k", limit=3, window=60) == 0.0


def test_disabled_never_limits():
    rl = RateLimiter(enabled=False)
    assert all(rl.check("k", limit=1, window=60) == 0.0 for _ in range(50))


def test_state_dict_is_bounded(monkeypatch):
    monkeypatch.setattr(ratelimit_mod, "_MAX_TRACKED", 8)
    rl = RateLimiter(enabled=True)
    for i in range(40):
        rl.check(f"k{i}", limit=1, window=60)
    assert len(rl._buckets) <= 8


# --- e2e: the 429 on each authenticated surface -----------------------------


@pytest.fixture(autouse=True)
def _live_limiter(monkeypatch):
    """This file exercises the limiter for real; conftest disables it globally.
    Start every test with an empty bucket dict."""
    ratelimiter._buckets.clear()
    monkeypatch.setattr(ratelimiter, "enabled", True)
    yield
    ratelimiter._buckets.clear()


def test_agent_surface_429_with_retry_after(client, monkeypatch):
    monkeypatch.setattr(settings, "ratelimit_agent_max", 3)
    _, token = create_host(client)

    for _ in range(3):
        assert client.post("/api/v1/agent/next-job", auth=signed(token)).status_code == 200

    r = client.post("/api/v1/agent/next-job", auth=signed(token))
    assert r.status_code == 429
    assert r.json()["detail"] == "rate limit exceeded"
    retry = int(r.headers["retry-after"])
    assert 1 <= retry <= settings.ratelimit_window_seconds


def test_admin_surface_429(client, monkeypatch):
    monkeypatch.setattr(settings, "ratelimit_admin_max", 3)
    ok = 0
    for _ in range(3):
        if client.get("/api/v1/admin/audit", headers=ADMIN_HEADERS).status_code == 200:
            ok += 1
    assert ok == 3
    r = client.get("/api/v1/admin/audit", headers=ADMIN_HEADERS)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1


def test_dashboard_surface_429(client, monkeypatch):
    monkeypatch.setattr(settings, "ratelimit_dashboard_max", 3)
    for _ in range(3):
        assert client.get("/api/v1/hosts").status_code == 200
    r = client.get("/api/v1/hosts")
    assert r.status_code == 429
    assert r.json()["detail"] == "rate limit exceeded"
    assert int(r.headers["retry-after"]) >= 1


def test_normal_volume_is_never_limited(client):
    _, token = create_host(client)
    for _ in range(5):
        assert client.get("/api/v1/hosts").status_code == 200
        assert (
            client.post("/api/v1/reports", auth=signed(token), json=report_payload()).status_code
            == 200
        )
        assert (
            client.get("/api/v1/admin/audit", headers=ADMIN_HEADERS).status_code == 200
        )


def test_429_is_captured_by_the_request_log(client, monkeypatch, caplog):
    monkeypatch.setattr(settings, "ratelimit_dashboard_max", 1)
    client.get("/api/v1/hosts")
    with caplog.at_level(logging.INFO):
        r = client.get("/api/v1/hosts")
    assert r.status_code == 429
    # log_requests wraps the rate-limit middleware, so the 429 is logged there,
    assert any(
        rec.name == "cadence.request" and rec.fields.get("status") == 429
        for rec in caplog.records
    )
    # and the dedicated warning fires too.
    assert any(
        rec.name == "cadence.ratelimit" and rec.fields.get("surface") == "dashboard"
        for rec in caplog.records
    )
