"""In-process throttle for repeated authentication failures.

This is not a security control on its own -- the admin key and host tokens
are compared in constant time already. It slows blind brute force against a
reachable port and, more usefully, makes a burst of failures visible in the
logs. State is per-process and best-effort (lost on restart, not shared
between workers). Set CADENCE_DISABLE_AUTH_THROTTLE=1 to turn it off (tests).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("cadence.auth")

_WINDOW_SECONDS = 300.0
_FREE_ATTEMPTS = 3          # first failures in the window are not delayed
_STEP_SECONDS = 0.25       # added delay per failure past the free ones
_MAX_SLEEP_SECONDS = 2.0   # cap so a worker thread is never tied up long
_MAX_TRACKED = 2048        # bound the state dict


@dataclass
class _Entry:
    count: int = 0
    last_seen: float = 0.0


class AuthThrottle:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._lock = threading.Lock()
        self._by_ip: dict[str, _Entry] = {}

    def record_failure(self, ip: str, *, kind: str) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        with self._lock:
            self._evict(now)
            entry = self._by_ip.get(ip)
            if entry is None or now - entry.last_seen > _WINDOW_SECONDS:
                entry = _Entry()
                self._by_ip[ip] = entry
            entry.count += 1
            entry.last_seen = now
            count = entry.count

        over = count - _FREE_ATTEMPTS
        if over <= 0:
            return
        delay = min(_MAX_SLEEP_SECONDS, _STEP_SECONDS * over)
        log.warning(
            "repeated %s auth failure from %s (%d in %ds); delaying %.2fs",
            kind, ip, count, int(_WINDOW_SECONDS), delay,
        )
        time.sleep(delay)

    def record_success(self, ip: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._by_ip.pop(ip, None)

    def _evict(self, now: float) -> None:
        if len(self._by_ip) < _MAX_TRACKED:
            return
        for ip in [
            ip for ip, e in self._by_ip.items() if now - e.last_seen > _WINDOW_SECONDS
        ]:
            del self._by_ip[ip]
        if len(self._by_ip) >= _MAX_TRACKED:
            oldest = sorted(self._by_ip.items(), key=lambda kv: kv[1].last_seen)
            for ip, _ in oldest[: len(oldest) // 2]:
                del self._by_ip[ip]


throttle = AuthThrottle(
    enabled=os.environ.get("CADENCE_DISABLE_AUTH_THROTTLE", "").strip() not in ("1", "true", "yes")
)


def client_ip(request) -> str:  # noqa: ANN001 -- starlette Request
    """Best-effort caller IP. The backend sits behind Caddy + nginx, so the
    real client is the first X-Forwarded-For hop when present."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
