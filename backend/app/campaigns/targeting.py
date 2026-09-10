"""Resolve a campaign's target hosts once, at creation.

Either an explicit `host_ids` list or a `tag` filter (the same "key" /
"key=value" matcher as `GET /hosts?tag=`, app.api.routes.hosts). The result is
a hostname-ordered list; the campaign's per-host stage assignment is frozen
from it and never recomputed, so a host that gains or loses the tag later does
not enter or leave a running campaign.
"""

from __future__ import annotations

import uuid

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from app.models.models import Host


def resolve_targets(
    db: Session, *, host_ids: list[uuid.UUID], tag: str | None
) -> list[Host]:
    """The hostname-ordered Host rows a campaign will act on. Raises ValueError
    if an explicit id is unknown (the route maps it to 422). A tag matching
    nothing returns [] -- the route turns that into 422 too."""
    ordered = select(Host).order_by(Host.hostname, Host.id)

    if host_ids:
        rows = db.execute(ordered.where(Host.id.in_(host_ids))).scalars().all()
        missing = [str(h) for h in host_ids if h not in {r.id for r in rows}]
        if missing:
            raise ValueError(f"unknown host id(s): {', '.join(missing)}")
        return list(rows)

    t = (tag or "").strip().lower()
    kv = func.jsonb_each_text(Host.tags).table_valued("key", "value")
    if "=" in t:
        key, value = t.split("=", 1)
        match = and_(func.lower(kv.c.key) == key, func.lower(kv.c.value) == value)
    else:
        like = f"%{t}%"
        match = or_(func.lower(kv.c.key).like(like), func.lower(kv.c.value).like(like))
    return list(
        db.execute(
            ordered.where(exists(select(1).select_from(kv).where(match)))
        )
        .scalars()
        .all()
    )


def slice_into_stages(host_count: int, stages: list) -> list[int]:
    """Map each hostname-ordered position (0..host_count-1) to a stage index.

    A stage size is an int (absolute), "N%" (floor of N percent of host_count),
    or "rest" (whatever is left). Each size is clamped to what remains. Raises
    ValueError if hosts are still unassigned after the last stage and there was
    no trailing "rest" -- the operator must cover the whole target set.
    """
    assignment: list[int] = []
    remaining = host_count
    for idx, spec in enumerate(stages):
        if spec == "rest":
            size = remaining
        elif isinstance(spec, str):  # "N%"
            size = host_count * int(spec[:-1]) // 100
        else:
            size = int(spec)
        size = max(0, min(size, remaining))
        assignment.extend([idx] * size)
        remaining -= size

    if remaining > 0:
        raise ValueError(
            f"stages cover {host_count - remaining} of {host_count} targeted "
            'hosts; add a final "rest" stage or increase the sizes'
        )
    return assignment
