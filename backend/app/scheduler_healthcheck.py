"""Scheduler service healthcheck: exit 0 if the loop's heartbeat is fresh.

The scheduler upserts a `last_tick_at` row in `scheduler_state` at the end of
every pass (app.scheduler.record_heartbeat). This check fails when that row is
missing or older than the timeout -- i.e. the loop is wedged (stuck tick, lost
database) even though PID 1 is still alive.

  CADENCE_SCHEDULER_HEARTBEAT_TIMEOUT   staleness threshold in seconds
                                        (default 180 = three missed ticks)
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, pool, select

from app.core.config import settings
from app.models.models import SchedulerState
from app.scheduler import HEARTBEAT_STATE_KEY

_TIMEOUT_SECONDS = int(os.environ.get("CADENCE_SCHEDULER_HEARTBEAT_TIMEOUT", "180"))


def main() -> int:
    engine = create_engine(settings.database_url, poolclass=pool.NullPool, future=True)
    try:
        with engine.connect() as conn:
            raw = conn.execute(
                select(SchedulerState.value).where(
                    SchedulerState.key == HEARTBEAT_STATE_KEY
                )
            ).scalar_one_or_none()
    finally:
        engine.dispose()

    if raw is None:
        print("no scheduler heartbeat recorded yet", file=sys.stderr)
        return 1
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        print(f"unparseable scheduler heartbeat: {raw!r}", file=sys.stderr)
        return 1

    age = (datetime.now(timezone.utc) - last).total_seconds()
    if age > _TIMEOUT_SECONDS:
        print(f"scheduler heartbeat stale: {age:.0f}s > {_TIMEOUT_SECONDS}s", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
