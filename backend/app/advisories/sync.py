"""Persist parsed advisories into `advisories` / `advisory_packages`. Each
advisory is upserted and its package rows are replaced wholesale, mirroring
how a host report replaces `host_packages` -- the feed is the source of truth
for what an advisory fixes.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.advisories.debian import ParsedAdvisory
from app.models.models import Advisory, AdvisoryPackage


def refresh_advisories(
    db: Session, parsed: list[ParsedAdvisory], now: datetime
) -> dict[str, int]:
    """Upsert every advisory in `parsed` and replace its `advisory_packages`
    rows. Returns {"advisories": n, "advisory_packages": m}. Advisories no
    longer in the feed are left in place -- a DSA is not retracted, and GC is
    a later concern. One commit for the whole batch.
    """
    adv_count = 0
    pkg_count = 0

    for adv in parsed:
        fields = {
            "source": adv.source,
            "url": adv.url,
            "title": adv.title,
            "cve_ids": adv.cve_ids,
            "published_at": adv.published_at,
            "updated_at": now,
        }
        db.execute(
            pg_insert(Advisory)
            .values(id=adv.id, **fields)
            .on_conflict_do_update(index_elements=["id"], set_=fields)
        )
        adv_count += 1

        db.execute(
            delete(AdvisoryPackage).where(AdvisoryPackage.advisory_id == adv.id)
        )
        # Dedupe on the composite PK in case a block lists a (release, package)
        # pair twice; last fixed_version wins.
        rows = {
            (release, package): fixed_version
            for release, package, fixed_version in adv.packages
        }
        if rows:
            db.execute(
                insert(AdvisoryPackage),
                [
                    {
                        "advisory_id": adv.id,
                        "release": release,
                        "package": package,
                        "fixed_version": fixed_version,
                    }
                    for (release, package), fixed_version in rows.items()
                ],
            )
            pkg_count += len(rows)

    db.commit()
    return {"advisories": adv_count, "advisory_packages": pkg_count}
