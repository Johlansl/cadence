"""Package exclusion (hold) rule resolution.

Patterns are operator input (globs, package_exclusions.pattern); the server
always resolves them to an exact list of package names before anything
reaches the agent -- a raw pattern never crosses the wire to a host (see
CLAUDE.md's closed-dispatch rule and docs/decisions.md "Open-source
posture"). resolve_for_job is the only entry point job creation needs.
"""

from __future__ import annotations

import fnmatch
import uuid
from collections.abc import Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.models import HostPackage, Package, PackageExclusion


def patterns_for_host(db: Session, host_id: uuid.UUID) -> list[str]:
    """Global + this host's own exclusion patterns. Additive: no re-inclusion,
    no override (decision 3) -- a host simply sees the union."""
    stmt = select(PackageExclusion.pattern).where(
        or_(PackageExclusion.scope == "global", PackageExclusion.host_id == host_id)
    )
    return list(db.execute(stmt).scalars().all())


def matching(names: Iterable[str], patterns: list[str]) -> list[str]:
    """Names that match at least one glob pattern, sorted and deduplicated.
    fnmatchcase (not fnmatch) keeps matching case-sensitive regardless of the
    server's OS locale -- Debian package names are always lowercase, and glob
    semantics should not vary with where Cadence happens to run."""
    if not patterns:
        return []
    seen = {n for n in names if any(fnmatch.fnmatchcase(n, p) for p in patterns)}
    return sorted(seen)


def resolve_for_job(db: Session, host_id: uuid.UUID) -> list[str]:
    """The exact package names to hold for an apt_upgrade job on host_id.

    Resolved against every package currently installed on the host, not just
    ones with a pending update: a hold must also apply pre-emptively to a
    package with no candidate today (e.g. `linux-image*`), so it is already in
    effect the moment a new version appears, rather than only from the next
    time this function happens to see a candidate_version for it."""
    patterns = patterns_for_host(db, host_id)
    if not patterns:
        return []
    names = (
        db.execute(
            select(Package.name)
            .join(HostPackage, HostPackage.package_id == Package.id)
            .where(HostPackage.host_id == host_id)
            .distinct()
        )
        .scalars()
        .all()
    )
    return matching(names, patterns)
