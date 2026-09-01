"""Keyset pagination helper for the "newest first" history endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.sql import ColumnElement


def before_keyset(
    ts_col: Any, id_col: Any, before: datetime | None, before_id: Any | None
) -> ColumnElement[bool] | None:
    """WHERE clause for the page after ``(before, before_id)`` on an
    ``ORDER BY ts_col DESC, id_col DESC`` scan.

    ``before`` alone keeps the plain strict-timestamp behaviour. Passing
    ``before_id`` as well adds the id tiebreaker, so rows that share a
    timestamp are never split across a page boundary (without it, the ones
    left on the far side of the cut are skipped for good). Returns ``None``
    when there is no cursor, so the caller can skip the filter entirely.
    """
    if before is None:
        return None
    if before_id is None:
        return ts_col < before
    return or_(ts_col < before, and_(ts_col == before, id_col < before_id))
