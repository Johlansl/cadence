"""The shared host-tag query matcher (app.tag_filter).

`tag_filter_clause` (the SQL side) is exercised end to end by
`test_hosts_pagination.py::test_tag_filter_key_and_key_value` and
`test_campaigns_api.py::test_create_with_tag_filter`; those staying green is the
equivalence proof for the routes/targeting refactor. Here we pin the grammar and
the pure-Python `tag_matches` mirror.
"""

from __future__ import annotations

from app.tag_filter import _parse_tag, tag_matches


def test_parse_tag_key_only():
    assert _parse_tag("role") == ("role", None)
    assert _parse_tag("  Role  ") == ("role", None)


def test_parse_tag_key_value():
    assert _parse_tag("role=web") == ("role", "web")
    assert _parse_tag("ROLE=Web") == ("role", "web")
    assert _parse_tag("role=") == ("role", "")
    assert _parse_tag("a=b=c") == ("a", "b=c")  # split on the first '=' only


def test_parse_tag_empty():
    # ("", None) is the deliberate match-all form; callers that must forbid an
    # empty query (PackageExclusionCreate) reject it before parsing.
    assert _parse_tag("") == ("", None)
    assert _parse_tag(None) == ("", None)
    assert _parse_tag("   ") == ("", None)


def test_tag_matches_key_present():
    assert tag_matches("role", {"role": "web"}) is True
    assert tag_matches("role", {"env": "prod"}) is False


def test_tag_matches_key_only_is_substring_on_key_or_value():
    assert tag_matches("we", {"role": "web"}) is True  # value substring
    assert tag_matches("rol", {"role": "web"}) is True  # key substring
    assert tag_matches("xyz", {"role": "web"}) is False


def test_tag_matches_exact_pair():
    assert tag_matches("role=web", {"role": "web"}) is True
    assert tag_matches("role=db", {"role": "web"}) is False
    assert tag_matches("role", {"role": "web", "env": "prod"}) is True


def test_tag_matches_case_insensitive_both_sides():
    assert tag_matches("ROLE=WEB", {"role": "web"}) is True
    assert tag_matches("role=web", {"Role": "Web"}) is True


def test_tag_matches_empty_query_is_match_all_for_a_tagged_host():
    # Mirrors the SQL LIKE '%%' + jsonb_each_text behaviour: any non-empty tag
    # set matches, an empty one does not.
    assert tag_matches("", {"role": "web"}) is True
    assert tag_matches("", {}) is False
