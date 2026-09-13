"""Tests for the cve_scores cache table (migration 0022, roadmap item 9)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.models import CveScore


def test_insert_scored_row(db_session):
    db_session.add(
        CveScore(
            cve_id="CVE-2024-0727",
            base_score=Decimal("5.5"),
            base_severity="MEDIUM",
            vector="CVSS:3.1/AV:L/AC:L/PR:N/UI:R/S:U/C:N/I:N/A:H",
            cvss_version="3.1",
            fetched_at=datetime(2026, 9, 13, tzinfo=timezone.utc),
        )
    )
    db_session.flush()

    row = db_session.get(CveScore, "CVE-2024-0727")
    assert row.base_score == Decimal("5.5")
    assert row.base_severity == "MEDIUM"
    assert row.source == "nvd"


def test_unknown_score_is_null_not_zero(db_session):
    db_session.add(CveScore(cve_id="CVE-2026-0000"))
    db_session.flush()

    row = db_session.get(CveScore, "CVE-2026-0000")
    assert row.base_score is None
    assert row.base_severity is None


def test_rerun_refresh_overwrites_the_row(db_session):
    db_session.add(CveScore(cve_id="CVE-2024-0727", base_severity="LOW"))
    db_session.flush()
    db_session.merge(
        CveScore(
            cve_id="CVE-2024-0727",
            base_score=Decimal("5.5"),
            base_severity="MEDIUM",
            fetched_at=datetime(2026, 9, 13, tzinfo=timezone.utc),
        )
    )
    db_session.flush()

    rows = db_session.execute(select(CveScore)).scalars().all()
    assert len(rows) == 1
    assert rows[0].base_severity == "MEDIUM"


def test_severity_check_rejects_unknown_label(db_session):
    db_session.add(CveScore(cve_id="CVE-2024-0727", base_severity="SEVERE"))
    with pytest.raises(IntegrityError):
        db_session.flush()
