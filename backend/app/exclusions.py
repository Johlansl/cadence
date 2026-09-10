"""Package exclusion (hold) rule resolution.

Patterns are operator input (globs, package_exclusions.pattern); the server
always resolves them to an exact list of package names before anything
reaches the agent -- a raw pattern never crosses the wire to a host (see
CLAUDE.md's closed-dispatch rule and docs/decisions.md "Open-source
posture"). Job creation needs two entry points: resolve_for_job (the wanted
set) and known_held_for_host (the last set Cadence itself recorded holding,
so the agent's reconciliation never touches a third-party hold).
"""

from __future__ import annotations

import fnmatch
import uuid
from collections.abc import Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.models import Host, HostPackage, Job, Package, PackageExclusion
from app.tag_filter import tag_matches


def patterns_for_host(
    db: Session, host_id: uuid.UUID, *, host_tags: dict | None = None
) -> list[str]:
    """Every exclusion pattern that applies to this host: the global rules, its
    own host rules, and every tag rule whose tag it carries. Additive across
    the three scopes -- no re-inclusion, no priority (decision 3), a host
    simply sees the union. Pass `host_tags` when the caller already holds the
    Host row, to skip the lookup."""
    if host_tags is None:
        host_tags = (
            db.execute(select(Host.tags).where(Host.id == host_id)).scalar_one_or_none()
            or {}
        )
    global_and_host = list(
        db.execute(
            select(PackageExclusion.pattern).where(
                or_(
                    PackageExclusion.scope == "global",
                    PackageExclusion.host_id == host_id,
                )
            )
        )
        .scalars()
        .all()
    )
    tag_rules = db.execute(
        select(PackageExclusion.tag, PackageExclusion.pattern).where(
            PackageExclusion.scope == "tag"
        )
    ).all()
    return global_and_host + [p for (t, p) in tag_rules if tag_matches(t, host_tags)]


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


def known_held_for_host(db: Session, host_id: uuid.UUID) -> list[str]:
    """The Cadence-managed hold set as of the most recent completed
    apt_upgrade job for this host (its result.held_packages), recomputed at
    read time -- same "no separate snapshot" pattern as security_updates_count
    / excluded_count. [] if no such job exists yet, or its result predates
    this field (an older agent): the safe default, since Cadence must never
    touch a hold it has no record of itself applying."""
    last = db.execute(
        select(Job.result)
        .where(
            Job.host_id == host_id,
            Job.job_type == "apt_upgrade",
            Job.status.in_(("succeeded", "failed")),
        )
        .order_by(Job.completed_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if not last:
        return []
    return last.get("held_packages") or []
