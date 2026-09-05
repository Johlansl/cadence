"""Unit tests for the Debian DSA/DLA feed parser (`app.advisories.debian`)."""

from __future__ import annotations

from datetime import datetime, timezone

from app.advisories.debian import DEBIAN_CODENAME, ParsedAdvisory, parse_list

SAMPLE = (
    "[04 Sep 2026] DSA-6483-1 thunderbird - security update\n"
    "\t{CVE-2026-16365 CVE-2026-16371 CVE-2026-75874}\n"
    "\t[trixie] - thunderbird 1:140.15.0esr-1~deb13u1\n"
    "[15 Aug 2026] DSA-5745-1 openssl - security update\n"
    "\t{CVE-2026-6119}\n"
    "\t[bookworm] - openssl 3.0.14-1~deb12u2\n"
    "\t[trixie] - openssl 3.3.1-2\n"
    "[10 Jul 2026] DSA-5700-1 somepkg - security update\n"
    "\t{}\n"
    "\t[bookworm] - somepkg <not-affected>\n"
    "\t[bullseye] - somepkg <no-dsa>\n"
    "[01 Jun 2026] DLA-3891-1 libfoo - security update\n"
    "\t{CVE-2026-1111 CVE-2026-2222}\n"
    "\t[bullseye] - libfoo 1.2.3-4+deb11u5\n"
    "NOTE: free-text lines like this are ignored\n"
)


def _by_id(text: str) -> dict[str, ParsedAdvisory]:
    return {a.id: a for a in parse_list(text)}


def test_parses_every_header():
    assert set(_by_id(SAMPLE)) == {
        "DSA-6483-1",
        "DSA-5745-1",
        "DSA-5700-1",
        "DLA-3891-1",
    }


def test_source_derived_from_id_prefix():
    got = _by_id(SAMPLE)
    assert got["DSA-5745-1"].source == "debian-dsa"
    assert got["DLA-3891-1"].source == "debian-dla"


def test_tracker_url():
    assert (
        _by_id(SAMPLE)["DSA-5745-1"].url
        == "https://security-tracker.debian.org/tracker/DSA-5745-1"
    )


def test_published_date_is_utc():
    assert _by_id(SAMPLE)["DSA-6483-1"].published_at == datetime(
        2026, 9, 4, tzinfo=timezone.utc
    )


def test_multi_release_fixed_versions():
    openssl = _by_id(SAMPLE)["DSA-5745-1"]
    assert openssl.packages == [
        ("bookworm", "openssl", "3.0.14-1~deb12u2"),
        ("trixie", "openssl", "3.3.1-2"),
    ]


def test_multi_cve_line_order_preserved():
    assert _by_id(SAMPLE)["DSA-6483-1"].cve_ids == [
        "CVE-2026-16365",
        "CVE-2026-16371",
        "CVE-2026-75874",
    ]


def test_placeholder_versions_skipped_advisory_still_emitted():
    adv = _by_id(SAMPLE)["DSA-5700-1"]
    assert adv.packages == []
    assert adv.cve_ids == []


def test_dla_block_and_note_lines_ignored():
    dla = _by_id(SAMPLE)["DLA-3891-1"]
    assert dla.packages == [("bullseye", "libfoo", "1.2.3-4+deb11u5")]
    assert dla.cve_ids == ["CVE-2026-1111", "CVE-2026-2222"]


def test_version_with_trailing_note_token():
    text = (
        "[01 Jan 2026] DSA-1000-1 bar - security update\n"
        "\t{CVE-2026-0001}\n"
        "\t[bookworm] - bar 2:1.0-1+deb12u1 (unimportant)\n"
    )
    assert parse_list(text)[0].packages == [("bookworm", "bar", "2:1.0-1+deb12u1")]


def test_empty_input():
    assert parse_list("") == []


def test_codename_map_covers_current_releases():
    assert DEBIAN_CODENAME["12"] == "bookworm"
    assert DEBIAN_CODENAME["13"] == "trixie"
