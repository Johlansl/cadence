"""Security-advisory enrichment: pull upstream advisory feeds (Debian DSA/DLA),
store them, and link a host's pending security updates to the advisories that
fix them. See `docs/decisions.md` ("Updates") for scope -- this augments apt's
own security flag, it is not a full vulnerability scan.
"""
