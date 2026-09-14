"""Generic OIDC client for admin SSO (roadmap item 10, commit 1: pure core).

Authorization Code Flow against any OIDC provider (reference: Authentik).
This module does discovery, JWKS handling, ID-token validation, session
sealing and actor derivation. HTTP entry points live in the API layer;
nothing here touches FastAPI, the database, or the process environment.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

log = logging.getLogger("cadence.oidc")

SESSION_PURPOSE = "cadence-session-v1"

# Header `alg` values this client verifies, nothing else. Compared byte for
# byte: "none", "None", "NONE" and every other spelling are rejected, and the
# header alg must additionally match the selected JWKS key type (alg
# confusion rejected even when the math would check out).
_ALLOWED_ALGS = ("RS256", "ES256")

_DISCOVERY_CACHE_SECONDS = 3600.0
_JWKS_CACHE_SECONDS = 3600.0

_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class OidcError(ValueError):
    """Anything wrong with an OIDC response, token, or session: callers turn
    this into a 401/400 without leaking which check failed to the client."""


def _f(**fields: object) -> dict:
    """Wrap structured fields for the logfmt formatter."""
    return {"fields": fields}


def _b64url_decode(segment: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (ValueError, binascii.Error) as exc:
        raise OidcError("malformed base64url") from exc


def fetch_json(url: str, *, timeout: float = 10.0, max_bytes: int = 64 * 1024) -> Any:
    """GET a JSON document, refusing bodies larger than `max_bytes` (metadata
    and JWKS are a few KB; the cap keeps a compromised endpoint from feeding
    unbounded input to the parser)."""
    req = urllib.request.Request(url, headers={"User-Agent": "cadence-backend"})
    chunks: list[bytes] = []
    total = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise OidcError(f"{url}: response exceeds {max_bytes} bytes")
            chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OidcError(f"{url}: not valid JSON") from exc


def _cached(cache: dict, key: str, ttl: float, loader) -> Any:
    now = time.monotonic()
    hit = cache.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    value = loader()
    cache[key] = (now, value)
    return value


def discovery(issuer: str) -> dict[str, Any]:
    """Fetch and cache the provider metadata for `issuer` (trailing slash
    tolerated). Requires the three endpoints the code flow needs."""
    normalized = issuer.rstrip("/")
    if not normalized.startswith("https://"):
        raise OidcError("issuer must be an https URL")

    def load() -> dict[str, Any]:
        metadata = fetch_json(normalized + "/.well-known/openid-configuration")
        if not isinstance(metadata, dict):
            raise OidcError("discovery document is not an object")
        for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not isinstance(metadata.get(field), str):
                raise OidcError(f"discovery document lacks {field}")
        return metadata

    return _cached(_discovery_cache, normalized, _DISCOVERY_CACHE_SECONDS, load)


def get_jwks(jwks_uri: str) -> dict[str, Any]:
    """Fetch and cache a JWKS document (rotation handled by re-fetching on
    unknown `kid` at the call site)."""

    def load() -> dict[str, Any]:
        jwks = fetch_json(jwks_uri)
        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
            raise OidcError("JWKS document has no keys list")
        return jwks

    return _cached(_jwks_cache, jwks_uri, _JWKS_CACHE_SECONDS, load)


def refresh_jwks(jwks_uri: str) -> dict[str, Any]:
    """Drop the cached JWKS for `jwks_uri` and fetch it again (key rotation:
    a `kid` the cache does not know gets exactly one re-fetch before the
    token is rejected)."""
    _jwks_cache.pop(jwks_uri, None)
    return get_jwks(jwks_uri)


def _b64url_uint(value: str) -> int:
    return int.from_bytes(_b64url_decode(value), "big")


def _public_key(jwk: dict[str, Any], alg: str):
    """Build a verify-only public key from one JWKS entry, refusing any key
    whose type does not match the token's `alg` (alg-confusion guard)."""
    kty = jwk.get("kty")
    if alg == "RS256":
        if kty != "RSA":
            raise OidcError("alg/kty mismatch")
        try:
            numbers = rsa.RSAPublicNumbers(
                _b64url_uint(jwk["e"]), _b64url_uint(jwk["n"])
            )
        except (KeyError, ValueError) as exc:
            raise OidcError("malformed RSA JWK") from exc
        return numbers.public_key()
    if jwk.get("crv") != "P-256":
        raise OidcError("unsupported EC curve")
    try:
        numbers = ec.EllipticCurvePublicNumbers(
            _b64url_uint(jwk["x"]), _b64url_uint(jwk["y"]), ec.SECP256R1()
        )
    except (KeyError, ValueError) as exc:
        raise OidcError("malformed EC JWK") from exc
    return numbers.public_key()


def select_jwk(jwks: dict[str, Any], kid: str | None, alg: str) -> dict[str, Any]:
    """Pick the verification key: exact `kid` match (a missing or unknown kid
    is rejected, never defaulted to a first or only key) and exact `alg`
    membership. The kty/alg cross-check happens in `_public_key`."""
    if alg not in _ALLOWED_ALGS:
        raise OidcError("unexpected alg")
    if not kid:
        raise OidcError("missing kid")
    for key in jwks.get("keys", []):
        if isinstance(key, dict) and key.get("kid") == kid:
            return key
    raise OidcError("unknown kid")


def _verify_signature(signing_input: bytes, signature: bytes, jwk: dict, alg: str) -> None:
    key = _public_key(jwk, alg)
    try:
        if alg == "RS256":
            key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
        else:
            if len(signature) != 64:
                raise OidcError("bad signature")
            r = int.from_bytes(signature[:32], "big")
            s = int.from_bytes(signature[32:], "big")
            key.verify(
                encode_dss_signature(r, s), signing_input, ec.ECDSA(hashes.SHA256())
            )
    except (InvalidSignature, ValueError) as exc:
        raise OidcError("bad signature") from exc


def parse_compact(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    """Split a JWS compact serialization into (header, claims, signing
    input, signature), checking structure and JSON shape only."""
    parts = token.split(".")
    if len(parts) != 3:
        raise OidcError("not a compact JWS")
    try:
        header = json.loads(_b64url_decode(parts[0]))
        claims = json.loads(_b64url_decode(parts[1]))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OidcError("malformed JWT JSON") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise OidcError("malformed JWT JSON")
    return header, claims, parts[0].encode() + b"." + parts[1].encode(), _b64url_decode(
        parts[2]
    )


def validate_id_token(
    token: str,
    *,
    jwks: dict[str, Any],
    issuer: str,
    client_id: str,
    nonce: str,
    now: int | None = None,
    skew_seconds: int = 120,
) -> dict[str, Any]:
    """Validate an ID token end to end and return its claims. Every failure
    raises `OidcError`: bad structure, unexpected `alg`, missing/unknown
    `kid`, bad signature, `iss` mismatch (exact), `aud` not containing this
    client (string or array form), expired or premature token, `nonce`
    mismatch."""
    header, claims, signing_input, signature = parse_compact(token)
    alg = header.get("alg")
    jwk = select_jwk(jwks, header.get("kid"), alg if isinstance(alg, str) else "")
    _verify_signature(signing_input, signature, jwk, alg)
    if claims.get("iss") != issuer:
        raise OidcError("iss mismatch")
    aud = claims.get("aud")
    audiences = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
    if client_id not in audiences:
        raise OidcError("aud mismatch")
    current = now if now is not None else int(time.time())
    exp = claims.get("exp")
    iat = claims.get("iat")
    if not isinstance(exp, int) or current >= exp + skew_seconds:
        raise OidcError("token expired")
    if isinstance(iat, int) and current < iat - skew_seconds:
        raise OidcError("token premature")
    if claims.get("nonce") != nonce:
        raise OidcError("nonce mismatch")
    return claims


def actor_from_claims(claims: dict[str, Any]) -> str:
    """Human-readable audit actor for a validated ID token: the email when
    present and verified, else `preferred_username`, else the opaque `sub`.
    The `sub` fallback is always a string so the audit column never sees a
    non-string."""
    email = claims.get("email")
    if isinstance(email, str) and email and claims.get("email_verified") is True:
        return email
    username = claims.get("preferred_username")
    if isinstance(username, str) and username:
        return username
    return str(claims.get("sub", "unknown"))


def seal_session(
    fernet_key: str, *, sub: str, email: str | None, name: str | None, ttl_seconds: int
) -> str:
    """Seal a session payload with Fernet (URL-safe token for the cookie).
    The purpose marker keeps session tokens disjoint from agent tokens even
    though they share the key; expiry lives inside the payload."""
    now = int(time.time())
    payload = {
        "v": SESSION_PURPOSE,
        "sub": sub,
        "email": email,
        "name": name,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return Fernet(fernet_key.encode()).encrypt(json.dumps(payload).encode()).decode()


def _canonical_raw(token: str) -> bytes | None:
    """Decode only if `token` is canonical base64url as Fernet emits it
    (padded): the stdlib decoder silently ignores trailing garbage, which
    would let a tampered token through when the ignored tail changes
    nothing (`token + "x"`). Re-encoding must reproduce the input byte for
    byte."""
    try:
        raw = base64.urlsafe_b64decode(token)
    except (ValueError, binascii.Error):
        return None
    if base64.urlsafe_b64encode(raw) != token.encode():
        return None
    return raw


def open_session(fernet_key: str, token: str) -> dict[str, Any] | None:
    """Open a session cookie: None when tampered, expired, or not a session
    token (never raises)."""
    if _canonical_raw(token) is None:
        return None
    try:
        payload = json.loads(Fernet(fernet_key.encode()).decrypt(token.encode()))
    except (InvalidToken, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != SESSION_PURPOSE:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or int(time.time()) >= exp:
        return None
    if not isinstance(payload.get("sub"), str) or not payload["sub"]:
        return None
    return payload
