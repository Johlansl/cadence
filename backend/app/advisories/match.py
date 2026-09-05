"""Link a host's pending security updates to the Debian advisories that fix
them. Matching is deliberately narrow: an apt-flagged security update is tied
to an advisory only when the advisory's per-release `fixed_version` is exactly
the candidate version apt offers -- no version-range comparison (see
`docs/decisions.md`, "Updates").
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from app.advisories.debian import DEBIAN_CODENAME
from app.models.models import Advisory, AdvisoryPackage

# Binary package -> Debian source package, for security-relevant libraries
# whose binary name differs from the source. Fallback only: agent 0.7.0 sends
# the real source name and this map is bypassed.
COMMON_BINARY_SOURCE: dict[str, str] = {
    "libssl3": "openssl",
    "libssl1.1": "openssl",
    "libcrypto3": "openssl",
    "libc6": "glibc",
    "locales": "glibc",
    "libcurl4": "curl",
    "libcurl3-gnutls": "curl",
    "zlib1g": "zlib",
    "libexpat1": "expat",
    "libsystemd0": "systemd",
    "libudev1": "systemd",
    "libgnutls30": "gnutls28",
    "libkrb5-3": "krb5",
    "libsqlite3-0": "sqlite3",
}


def source_for(binary_name: str) -> str:
    """Best-effort binary -> Debian source package name."""
    return COMMON_BINARY_SOURCE.get(binary_name, binary_name)


def codename_for(os_version: str | None) -> str | None:
    """Resolve a host's reported OS version to a Debian release codename.
    A bare major number we don't know maps to None (no advisory data);
    a non-numeric value is assumed to already be a codename."""
    if not os_version:
        return None
    if os_version in DEBIAN_CODENAME:
        return DEBIAN_CODENAME[os_version]
    if os_version.isdigit():
        return None
    return os_version


def advisories_for(
    db: Session, items: Iterable[tuple[Any, str, str, str]]
) -> dict[Any, list[dict]]:
    """`items` = (key, source_package, release_codename, candidate_version)
    tuples, one per security-flagged pending update. Returns
    ``{key: [{"id", "url", "cves"}, ...]}`` for the keys that matched at least
    one advisory (unmatched keys are absent). Newest advisory first.
    """
    items = list(items)
    if not items:
        return {}

    pairs = sorted({(src, rel) for _key, src, rel, _cand in items})
    rows = db.execute(
        select(
            AdvisoryPackage.package,
            AdvisoryPackage.release,
            AdvisoryPackage.fixed_version,
            Advisory.id,
            Advisory.url,
            Advisory.cve_ids,
            Advisory.published_at,
        )
        .join(Advisory, Advisory.id == AdvisoryPackage.advisory_id)
        .where(tuple_(AdvisoryPackage.package, AdvisoryPackage.release).in_(pairs))
    ).all()

    by_fix: dict[tuple[str, str, str], list] = {}
    for r in rows:
        by_fix.setdefault((r.package, r.release, r.fixed_version), []).append(r)

    out: dict[Any, list[dict]] = {}
    for key, src, rel, candidate in items:
        matches = by_fix.get((src, rel, candidate))
        if not matches:
            continue
        ordered = sorted(
            matches,
            key=lambda r: (r.published_at is not None, r.published_at),
            reverse=True,
        )
        seen: set[str] = set()
        refs: list[dict] = []
        for r in ordered:
            if r.id in seen:
                continue
            seen.add(r.id)
            refs.append({"id": r.id, "url": r.url, "cves": list(r.cve_ids or [])})
        out[key] = refs
    return out
