"""Fetch and parse Debian security-advisory feeds -- `data/DSA/list` and
`data/DLA/list` from the security-tracker. Pure text handling plus a
size-capped stdlib fetch; nothing here touches the database (the scheduler
wires this to `app.advisories.sync`).

Feed entry shape::

    [04 Sep 2026] DSA-6483-1 thunderbird - security update
    <TAB>{CVE-2026-16365 CVE-2026-16371}
    <TAB>[trixie] - thunderbird 1:140.15.0esr-1~deb13u1

A block starts at a header line; the indented lines that follow carry the CVE
id list (once, possibly empty `{}`) and one `[release] - source version` line
per fixed release. A release line whose version token is a placeholder
(`<not-affected>`, `<unfixed>`, `<no-dsa>`, ...) carries no fix and is skipped.
"""

from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

# VERSION_ID (from a host's /etc/os-release) -> Debian release codename. A
# value that is already a codename passes straight through in the read path.
DEBIAN_CODENAME = {
    "11": "bullseye",
    "12": "bookworm",
    "13": "trixie",
    "14": "forky",
}

# Default feeds: the stable-security DSA list and the LTS DLA list.
DEFAULT_FEED_URLS = (
    "https://salsa.debian.org/security-tracker-team/security-tracker/-/raw/master/data/DSA/list",
    "https://salsa.debian.org/security-tracker-team/security-tracker/-/raw/master/data/DLA/list",
)

_TRACKER = "https://security-tracker.debian.org/tracker/"

_HEADER = re.compile(
    r"^\[(?P<date>\d{1,2} \w{3} \d{4})\] "
    r"(?P<id>(?:DSA|DLA)-\d+-\d+) "
    r"(?P<pkg>\S+) - (?P<title>.+)$"
)
_CVES = re.compile(r"^\s+\{(?P<cves>[^}]*)\}\s*$")
_RELEASE = re.compile(r"^\s+\[(?P<release>[^\]]+)\] - (?P<pkg>\S+)\s+(?P<version>\S+)")

_PLACEHOLDER_VERSIONS = frozenset(
    (
        "<not-affected>",
        "<unfixed>",
        "<no-dsa>",
        "<removed>",
        "<end-of-life>",
        "<undetermined>",
        "<postponed>",
    )
)


@dataclass
class ParsedAdvisory:
    id: str
    source: str  # 'debian-dsa' | 'debian-dla'
    url: str
    title: str
    cve_ids: list[str] = field(default_factory=list)
    published_at: datetime | None = None
    # (release codename, source package, fixed version)
    packages: list[tuple[str, str, str]] = field(default_factory=list)


def _source_for(advisory_id: str) -> str:
    return "debian-dla" if advisory_id.startswith("DLA-") else "debian-dsa"


def _parse_date(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw, "%d %b %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_list(text: str) -> list[ParsedAdvisory]:
    """Parse a DSA/list or DLA/list file. Unrecognised lines are ignored; a
    block with no usable release line still yields an advisory (empty
    `packages`) so its CVE list is not lost."""
    advisories: list[ParsedAdvisory] = []
    current: ParsedAdvisory | None = None

    for line in text.splitlines():
        header = _HEADER.match(line)
        if header:
            current = ParsedAdvisory(
                id=header["id"],
                source=_source_for(header["id"]),
                url=f"{_TRACKER}{header['id']}",
                title=header["title"].strip(),
                published_at=_parse_date(header["date"]),
            )
            advisories.append(current)
            continue

        if current is None:
            continue

        cves = _CVES.match(line)
        if cves:
            for token in cves["cves"].split():
                if token.startswith("CVE-") and token not in current.cve_ids:
                    current.cve_ids.append(token)
            continue

        release = _RELEASE.match(line)
        if release and release["version"] not in _PLACEHOLDER_VERSIONS:
            current.packages.append(
                (release["release"], release["pkg"], release["version"])
            )

    return advisories


def fetch(url: str, *, timeout: float = 30.0, max_bytes: int = 16 * 1024 * 1024) -> str:
    """GET `url` as text, refusing a body larger than `max_bytes`. The cap is a
    deliberate tripwire: the DSA/DLA lists are ~1-2 MB, so hitting it means the
    URL now points at something much larger (e.g. the tracker JSON) and the
    caller must change on purpose."""
    req = urllib.request.Request(url, headers={"User-Agent": "cadence-scheduler"})
    chunks: list[bytes] = []
    total = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"{url}: response exceeds {max_bytes} bytes")
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")
