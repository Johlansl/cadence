"""Keyset pagination helpers.

``before_keyset`` walks a ``DESC`` scan (the "newest first" history
endpoints); ``after_keyset`` walks an ``ASC`` scan (the host list, ordered by
hostname).
"""

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


def after_keyset(
    sort_col: Any, id_col: Any, after: Any | None, after_id: Any | None
) -> ColumnElement[bool] | None:
    """WHERE clause for the page after ``(after, after_id)`` on an
    ``ORDER BY sort_col ASC, id_col ASC`` scan -- the mirror of
    ``before_keyset``. The id tiebreaker keeps rows that share a
    ``sort_col`` value from being split across a page boundary. Returns
    ``None`` when there is no cursor.
    """
    if after is None:
        return None
    if after_id is None:
        return sort_col > after
    return or_(sort_col > after, and_(sort_col == after, id_col > after_id))
