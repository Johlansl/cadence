import ipaddress
import time

from starlette.requests import Request

from app.core import throttle as throttle_mod
from app.core.config import settings
from app.core.throttle import AuthThrottle, client_ip


def test_free_attempts_then_growing_delay():
    t = AuthThrottle(enabled=True)

    early = [
        t.record_failure("10.0.0.1", kind="admin-key")
        for _ in range(throttle_mod._FREE_ATTEMPTS)
    ]
    assert early == [0.0] * throttle_mod._FREE_ATTEMPTS  # first failures are free

    d1 = t.record_failure("10.0.0.1", kind="admin-key")
    d2 = t.record_failure("10.0.0.1", kind="admin-key")
    assert 0 < d1 < d2 <= throttle_mod._MAX_DELAY_SECONDS


def test_record_failure_never_blocks():
    """The backoff is returned, not slept -- a synchronous sleep here would
    tie up a thread-pool worker on the request path."""
    t = AuthThrottle(enabled=True)
    for _ in range(throttle_mod._FREE_ATTEMPTS + 5):
        t.record_failure("10.0.0.9", kind="admin-key")

    start = time.monotonic()
    delay = t.record_failure("10.0.0.9", kind="admin-key")
    assert time.monotonic() - start < 0.05  # returned immediately
    assert delay > 0  # ... but a delay is owed to the caller


def test_success_resets_the_counter():
    t = AuthThrottle(enabled=True)

    last = 0.0
    for _ in range(throttle_mod._FREE_ATTEMPTS + 2):
        last = t.record_failure("10.0.0.2", kind="signed")
    assert last > 0  # was being delayed

    t.record_success("10.0.0.2")
    after = [
        t.record_failure("10.0.0.2", kind="signed")
        for _ in range(throttle_mod._FREE_ATTEMPTS)
    ]
    assert after == [0.0] * throttle_mod._FREE_ATTEMPTS  # back under the free threshold


def test_disabled_throttle_returns_no_delay():
    t = AuthThrottle(enabled=False)
    assert all(
        t.record_failure("10.0.0.3", kind="admin-key") == 0.0 for _ in range(50)
    )


def test_per_ip_buckets():
    t = AuthThrottle(enabled=True)
    delays = [
        t.record_failure(f"10.0.1.{i}", kind="admin-key")
        for i in range(throttle_mod._FREE_ATTEMPTS)
    ]
    assert delays == [0.0] * throttle_mod._FREE_ATTEMPTS  # each IP has its own allowance


# --- client_ip: X-Forwarded-For is only believed behind a trusted proxy ----


def _req(peer: str, xff: str | None = None) -> Request:
    headers = [] if xff is None else [(b"x-forwarded-for", xff.encode())]
    return Request({"type": "http", "headers": headers, "client": (peer, 12345)})


def _trust(monkeypatch, *cidrs: str) -> None:
    monkeypatch.setattr(
        settings, "trusted_proxies", [ipaddress.ip_network(c) for c in cidrs]
    )


def test_xff_ignored_without_a_trusted_proxy(monkeypatch):
    _trust(monkeypatch)  # empty -> fail-safe
    assert client_ip(_req("172.20.0.9", xff="1.2.3.4")) == "172.20.0.9"


def test_xff_used_when_peer_is_trusted(monkeypatch):
    _trust(monkeypatch, "172.20.0.0/16")
    assert client_ip(_req("172.20.0.9", xff="1.2.3.4")) == "1.2.3.4"


def test_xff_walks_past_further_trusted_hops(monkeypatch):
    _trust(monkeypatch, "172.20.0.0/16")
    assert client_ip(_req("172.20.0.9", xff="1.2.3.4, 172.20.0.5")) == "1.2.3.4"


def test_xff_ignored_when_peer_is_not_trusted(monkeypatch):
    _trust(monkeypatch, "172.20.0.0/16")
    assert client_ip(_req("203.0.113.1", xff="1.2.3.4")) == "203.0.113.1"


def test_trusted_peer_without_xff_returns_peer(monkeypatch):
    _trust(monkeypatch, "172.20.0.0/16")
    assert client_ip(_req("172.20.0.9")) == "172.20.0.9"


def test_all_hops_trusted_falls_back_to_peer(monkeypatch):
    _trust(monkeypatch, "172.20.0.0/16")
    assert client_ip(_req("172.20.0.9", xff="172.20.0.3, 172.20.0.5")) == "172.20.0.9"


def test_garbage_left_of_the_real_client_is_skipped(monkeypatch):
    _trust(monkeypatch, "172.20.0.0/16")
    assert client_ip(_req("172.20.0.9", xff="junk, 1.2.3.4")) == "1.2.3.4"


def test_repeated_bad_admin_key_still_returns_401_when_throttled(client, monkeypatch):
    """End to end: with the throttle enabled the delayed branch is an
    `await asyncio.sleep`, so the 401 still comes back."""
    from app.core.throttle import throttle

    monkeypatch.setattr(throttle, "enabled", True)
    try:
        for _ in range(throttle_mod._FREE_ATTEMPTS + 2):
            r = client.post(
                "/api/v1/admin/hosts",
                headers={"X-Admin-Key": "wrong"},
                json={"hostname": "x"},
            )
            assert r.status_code == 401
    finally:
        throttle.record_success("testclient")


def test_correct_admin_key_clears_the_backoff(client, monkeypatch):
    from tests.conftest import ADMIN_KEY

    monkeypatch.setattr(throttle_mod.throttle, "enabled", True)
    try:
        for _ in range(throttle_mod._FREE_ATTEMPTS + 3):
            client.post(
                "/api/v1/admin/hosts",
                headers={"X-Admin-Key": "wrong"},
                json={"hostname": "x"},
            )

        r = client.post(
            "/api/v1/admin/hosts",
            headers={"X-Admin-Key": ADMIN_KEY},
            json={"hostname": "reset-check"},
        )
        assert r.status_code == 201  # correct key works and clears the bucket

        start = time.monotonic()
        client.post(
            "/api/v1/admin/hosts", headers={"X-Admin-Key": "wrong"}, json={"hostname": "x"}
        )
        assert time.monotonic() - start < 0.2  # back inside the free allowance
    finally:
        throttle_mod.throttle.record_success("testclient")
