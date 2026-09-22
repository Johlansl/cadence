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
import urllib.error
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
STATE_PURPOSE = "cadence-oidc-state-v1"

# Short life for the login-flow state: long enough to type a password at
# the provider, short enough to bound replay.
STATE_TTL_SECONDS = 600

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


class UnknownKidError(OidcError):
    """The token's `kid` matches no cached JWKS entry: exactly one refresh
    is warranted before rejecting (key rotation), unlike every other
    failure, which is final."""


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
    unbounded input to the parser). Transport failures (DNS, refused,
    timeouts, TLS, HTTP error statuses) surface as `OidcError`, never raw:
    the routes map that to 502/401 without a 500 or a traceback."""
    req = urllib.request.Request(url, headers={"User-Agent": "cadence-backend"})
    chunks: list[bytes] = []
    total = 0
    try:
        resp_cm = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise OidcError(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        # OSError covers socket.gaierror, refused connections, timeouts and
        # ssl.SSLError; HTTPError subclasses URLError and is handled above
        # for a status-specific message.
        raise OidcError(f"{url}: unreachable") from exc
    with resp_cm as resp:
        while True:
            try:
                chunk = resp.read(65536)
            except (urllib.error.URLError, OSError) as exc:
                raise OidcError(f"{url}: body unreadable") from exc
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


def _normalize_issuer(value: Any) -> Any:
    """Issuer comparison form: a single trailing slash is insignificant.

    Applied symmetrically to both sides of every issuer comparison (never
    to one side alone), so `"https://host/app"` and `"https://host/app/"`
    name the same provider whichever side carries the slash. Nothing else
    is folded: distinct paths or hosts still mismatch. Non-strings pass
    through unchanged so a missing/non-string `iss` still fails closed.
    """
    if isinstance(value, str) and value.endswith("/"):
        return value[:-1]
    return value


def discovery(issuer: str) -> dict[str, Any]:
    """Fetch and cache the provider metadata for `issuer` (trailing slash
    tolerated). Requires the three endpoints the code flow needs."""
    normalized = _normalize_issuer(issuer)
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
    raise UnknownKidError("unknown kid")


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
    `kid`, bad signature, `iss` mismatch (one trailing slash tolerated on
    either side, nothing else), `aud` not containing this client (string
    or array form), expired or premature token, `nonce` mismatch."""
    header, claims, signing_input, signature = parse_compact(token)
    alg = header.get("alg")
    jwk = select_jwk(jwks, header.get("kid"), alg if isinstance(alg, str) else "")
    _verify_signature(signing_input, signature, jwk, alg)
    if _normalize_issuer(claims.get("iss")) != _normalize_issuer(issuer):
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


def seal_token(fernet_key: str, purpose: str, data: dict[str, Any], ttl_seconds: int) -> str:
    """Seal an arbitrary payload with Fernet (URL-safe token for a cookie).
    The purpose marker keeps token kinds disjoint even though they share the
    key; expiry lives inside the payload."""
    now = int(time.time())
    payload = {"v": purpose, "iat": now, "exp": now + ttl_seconds, **data}
    return Fernet(fernet_key.encode()).encrypt(json.dumps(payload).encode()).decode()


def open_token(fernet_key: str, purpose: str, token: str) -> dict[str, Any] | None:
    """Open a sealed cookie token: None when tampered, expired, or of another
    purpose (never raises)."""
    if _canonical_raw(token) is None:
        return None
    try:
        payload = json.loads(Fernet(fernet_key.encode()).decrypt(token.encode()))
    except (InvalidToken, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != purpose:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or int(time.time()) >= exp:
        return None
    return payload


def seal_session(
    fernet_key: str,
    *,
    sub: str,
    email: str | None,
    name: str | None,
    ttl_seconds: int,
    actor: str | None = None,
    role: str | None = None,
) -> str:
    """Seal a session payload (URL-safe token for the cookie). The resolved
    audit actor is sealed alongside so request handling never re-derives it
    (and cannot disagree with what login saw); the RBAC role rides along
    the same way (absent on pre-RBAC cookies, which read as reader)."""
    data: dict[str, Any] = {"sub": sub, "email": email, "name": name}
    if actor is not None:
        data["actor"] = actor
    if role is not None:
        data["role"] = role
    return seal_token(fernet_key, SESSION_PURPOSE, data, ttl_seconds)


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
    """Open a session cookie: None when tampered, expired, not a session
    token, or missing its subject (never raises)."""
    payload = open_token(fernet_key, SESSION_PURPOSE, token)
    if payload is None:
        return None
    if not isinstance(payload.get("sub"), str) or not payload["sub"]:
        return None
    return payload


def exchange_code(
    token_endpoint: str,
    *,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str,
    timeout: float = 10.0,
    max_bytes: int = 64 * 1024,
) -> dict[str, Any]:
    """Exchange an authorization code for tokens (confidential client, HTTP
    Basic with the client credentials, form-encoded as the spec requires).
    Returns the decoded token response; any provider error raises."""
    body = urllib.parse.urlencode(
        {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
    ).encode()
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(
        token_endpoint,
        data=body,
        headers={
            "User-Agent": "cadence-backend",
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {credentials}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise OidcError(f"token endpoint HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        # OSError covers refused connections, stalls mid-body, timeouts and
        # ssl.SSLError; HTTPError subclasses URLError and keeps its branch.
        raise OidcError(f"token endpoint unreachable: {exc}") from exc
    if len(raw) > max_bytes:
        raise OidcError("token response too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OidcError("token response is not JSON") from exc
    if not isinstance(data, dict):
        raise OidcError("token response is not an object")
    if "error" in data:
        raise OidcError(f"token endpoint error: {data.get('error')}")
    if not isinstance(data.get("id_token"), str):
        raise OidcError("token response has no id_token")
    return data


def build_authorize_url(
    authorization_endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    nonce: str,
) -> str:
    """Authorization redirect target for the login entry point."""
    return authorization_endpoint + "?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "nonce": nonce,
        }
    )
