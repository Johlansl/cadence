"""RBAC v1 roles for OIDC accounts.

Two roles only: a reader observes (public reads plus the reads that need
an authenticated caller), an operator does everything the shared
X-Admin-Key does, under a nominative identity. The key itself stays a
total bypass outside this model (break-glass, option (a)).

Membership is static config (`CADENCE_OIDC_OPERATOR_EMAILS`), resolved at
login and sealed into the session next to the actor: there is no users
table, so a role change applies at the next login (bounded by the 8 h
session TTL). Permissions are named strings checked by one shared core
(`app.api.deps`), never a boolean, so finer splits later do not rewrite
the call sites.
"""

from __future__ import annotations

from typing import Any

READER = "reader"
OPERATOR = "operator"

# Named permissions checked by the shared dependency core. v1 needs two:
# every write takes OPERATE, the reads that require an authenticated caller
# take READ_PRIVATE. Public reads take none.
OPERATE = "operate"
READ_PRIVATE = "read_private"


def normalize_identity(value: Any) -> str | None:
    """Lowercase stripped identity, None when blank or not a string, so
    `Op@Example.test` and `op@example.test` are the same operator."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def resolve_role(
    *, actor: str | None, email: str | None, operators: list[str]
) -> str:
    """Role for a fresh login. Operator when the actor or the email claim
    matches the configured allowlist (both normalized); reader otherwise.
    The email is matched even when unverified on purpose: the reference
    provider reports `email_verified: false` by default, and the key stays
    available as break-glass either way."""
    allowed = {entry for entry in (normalize_identity(e) for e in operators) if entry}
    candidates = {normalize_identity(actor), normalize_identity(email)} - {None}
    if allowed and candidates & allowed:
        return OPERATOR
    return READER


def role_from_session(session: dict[str, Any] | None) -> str:
    """Role sealed in a session cookie. A missing or unknown value (cookies
    sealed before RBAC existed) reads as reader: fail closed."""
    if not isinstance(session, dict):
        return READER
    role = session.get("role")
    return role if role in (READER, OPERATOR) else READER
