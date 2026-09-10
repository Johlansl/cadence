"""Shared host-tag query matching.

`hosts.tags` is a flat JSONB `{key: value}` map. Two places filter hosts by a
tag query string in the same "key" / "key=value" shape:

- `GET /api/v1/hosts?tag=` (`app.api.routes.hosts.list_hosts`),
- campaign targeting (`app.campaigns.targeting.resolve_targets`),

and a third now needs the same rule the other way round: resolving which
`scope='tag'` package-exclusion rules apply to one given host
(`app.exclusions.patterns_for_host`). The query grammar and its case-folding
must not drift between them, so it lives here once.

`tag_filter_clause` builds the SQL predicate for a `Host`-row query (the host
set is unbounded / paginated); `tag_matches` is the pure-Python equivalent for
the fixed-host / varying-rule direction (avoids a correlated subquery per rule).
Both go through `_parse_tag`, so "key" vs "key=value" is decided in one spot.
"""

from __future__ import annotations

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.sql.elements import ColumnElement

from app.models.models import Host


def _parse_tag(raw: str | None) -> tuple[str, str | None]:
    """Normalise a tag query to `(key, value)`.

    `strip().lower()`, then split on the first `=`. A query with no `=` yields
    `(key, None)` (the "key present" form); `""` yields `("", None)`, which both
    matchers below treat as match-all. Callers that must forbid an empty query
    (`PackageExclusionCreate`) reject it before calling here.
    """
    t = (raw or "").strip().lower()
    if "=" in t:
        key, value = t.split("=", 1)
        return key, value
    return t, None


def tag_filter_clause(tag: str) -> ColumnElement[bool]:
    """A boolean SQL expression for `select(Host).where(...)`.

    `"key=value"` -> the host has that exact pair (case-insensitive).
    `"key"` -> some tag key or value contains it as a substring
    (case-insensitive). `""` -> the host has at least one tag. `func.lower()`
    stays on the stored side so a tag written before host-tag lowercasing (or
    inserted raw) still matches.
    """
    key, value = _parse_tag(tag)
    kv = func.jsonb_each_text(Host.tags).table_valued("key", "value")
    if value is not None:
        match = and_(func.lower(kv.c.key) == key, func.lower(kv.c.value) == value)
    else:
        like = f"%{key}%"
        match = or_(func.lower(kv.c.key).like(like), func.lower(kv.c.value).like(like))
    return exists(select(1).select_from(kv).where(match))


def tag_matches(query: str, tags: dict[str, str]) -> bool:
    """Pure-Python mirror of `tag_filter_clause`: does `query` select a host
    carrying `tags`? Same grammar, same case-insensitivity, same empty-query
    match-all (which is only reachable here from a caller that allows it)."""
    key, value = _parse_tag(query)
    items = [(k.lower(), v.lower()) for k, v in tags.items()]
    if value is not None:
        return any(k == key and v == value for k, v in items)
    return any(key in k or key in v for k, v in items)
