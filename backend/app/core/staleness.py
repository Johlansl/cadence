"""How stale a host's ``last_seen_at`` is allowed to get before the dashboard
flags it. Shared by the fleet summary and the host-list freshness filter.
Kept in sync with ``frontend/src/lib/time.ts``.
"""

from __future__ import annotations

from datetime import timedelta

LATE_AFTER = timedelta(minutes=5)
SILENT_AFTER = timedelta(minutes=15)
