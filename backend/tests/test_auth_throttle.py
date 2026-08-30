from app.core import throttle as throttle_mod
from app.core.throttle import AuthThrottle


def test_free_attempts_then_growing_delay(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(throttle_mod.time, "sleep", slept.append)
    t = AuthThrottle(enabled=True)

    for _ in range(throttle_mod._FREE_ATTEMPTS):
        t.record_failure("10.0.0.1", kind="admin-key")
    assert slept == []  # first failures are not delayed

    t.record_failure("10.0.0.1", kind="admin-key")
    t.record_failure("10.0.0.1", kind="admin-key")
    assert len(slept) == 2
    assert 0 < slept[0] < slept[1] <= throttle_mod._MAX_SLEEP_SECONDS


def test_success_resets_the_counter(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(throttle_mod.time, "sleep", slept.append)
    t = AuthThrottle(enabled=True)

    for _ in range(throttle_mod._FREE_ATTEMPTS + 2):
        t.record_failure("10.0.0.2", kind="bearer")
    assert slept  # was being delayed

    t.record_success("10.0.0.2")
    slept.clear()
    for _ in range(throttle_mod._FREE_ATTEMPTS):
        t.record_failure("10.0.0.2", kind="bearer")
    assert slept == []  # back under the free threshold


def test_disabled_throttle_never_sleeps(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(throttle_mod.time, "sleep", slept.append)
    t = AuthThrottle(enabled=False)

    for _ in range(50):
        t.record_failure("10.0.0.3", kind="admin-key")
    assert slept == []


def test_per_ip_buckets(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(throttle_mod.time, "sleep", slept.append)
    t = AuthThrottle(enabled=True)

    for i in range(throttle_mod._FREE_ATTEMPTS):
        t.record_failure(f"10.0.1.{i}", kind="admin-key")
    assert slept == []  # each IP still within its own free allowance
