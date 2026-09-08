"""In-process volume cap on successful authenticated traffic.

Separate from app.core.throttle, which counts *auth failures* and returns a
growing backoff to slow brute force. This one counts *every* request on an
already-authenticated surface against a hard per-window cap and tells the
caller to answer 429. A valid admin key or agent token is otherwise
unmetered.

Fixed window, per-process (one Uvicorn worker), best-effort (lost on restart).
`check` only does bookkeeping and returns the Retry-After the caller owes;
it never sleeps and never raises. Set CADENCE_RATELIMIT_ENABLED=false to turn
it off (it is on by default).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass

from app.core.config import settings

log = logging.getLogger("cadence.ratelimit")

_MAX_TRACKED = 4096  # bound the state dict


def note_rejected(surface: str, key_label: str, limit: int, retry_after: float, path: str) -> None:
    """One structured warning per rejected request. `key_label` must already be
    safe to log (an IP, or a token_hash prefix -- never a raw secret)."""
    log.warning(
        "rate limit exceeded",
        extra={
            "fields": {
                "surface": surface,
                "key": key_label,
                "limit": limit,
                "retry_after": retry_after,
                "path": path,
            }
        },
    )


@dataclass
class _Bucket:
    count: int = 0
    window_start: float = 0.0  # time.monotonic() of the first hit in the window


class RateLimiter:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

    def check(self, key: str, *, limit: int, window: float) -> float:
        """Count one request for `key`. Return 0.0 if it is within `limit` per
        `window` seconds; otherwise the Retry-After in seconds (>= 1), the time
        left until the current window rolls over. Never sleeps, never raises."""
        if not self.enabled:
            return 0.0
        now = time.monotonic()
        with self._lock:
            self._evict(now, window)
            bucket = self._buckets.get(key)
            if bucket is None or now - bucket.window_start >= window:
                bucket = _Bucket(count=0, window_start=now)
                self._buckets[key] = bucket
            if bucket.count <= limit:  # cap the counter so a flood stays bounded
                bucket.count += 1
            over = bucket.count > limit
            window_start = bucket.window_start
        if not over:
            return 0.0
        retry_after = math.ceil(window_start + window - now)
        return float(min(max(retry_after, 1), int(window)))

    def _evict(self, now: float, window: float) -> None:
        if len(self._buckets) < _MAX_TRACKED:
            return
        for key in [
            k for k, b in self._buckets.items() if now - b.window_start >= window
        ]:
            del self._buckets[key]
        if len(self._buckets) >= _MAX_TRACKED:
            oldest = sorted(self._buckets.items(), key=lambda kv: kv[1].window_start)
            for key, _ in oldest[: len(oldest) // 2]:
                del self._buckets[key]


ratelimiter = RateLimiter(enabled=settings.ratelimit_enabled)
