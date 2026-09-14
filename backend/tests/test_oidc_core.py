"""Unit tests for app.auth.oidc (roadmap item 10, commit 1: pure core).

Tokens are minted in-test with self-signed RSA/EC keys, so no test touches
the network. The adversarial vectors mirror the approved validation
checklist: bad structure, unexpected alg (including case variants of none),
alg confusion across key types, missing/unknown/misbound kid, tampered
payload, iss/aud mismatches (aud as string and as array, one trailing
slash in iss tolerated symmetrically), expiry, premature iat, and nonce
mismatch.
"""

from __future__ import annotations

import base64
import json
import socket
import time
import urllib.error
import urllib.request

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app.auth.oidc import (
    OidcError,
    actor_from_claims,
    exchange_code,
    fetch_json,
    open_session,
    parse_compact,
    seal_session,
    select_jwk,
    validate_id_token,
)

ISSUER = "https://auth.example.com/application/o/cadence/"
CLIENT_ID = "cadence-dashboard"
NONCE = "test-nonce-123"
NOW = 1780000000

RSA_KID = "rsa-key-1"
EC_KID = "ec-key-1"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_uint(value: int, length: int) -> str:
    return _b64url(value.to_bytes(length, "big"))


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def ec_key():
    return ec.generate_private_key(ec.SECP256R1())


def _rsa_jwk(key, kid: str) -> dict:
    numbers = key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _b64url(numbers.n.to_bytes(256, "big")),
        "e": _b64url(numbers.e.to_bytes(3, "big")),
    }


def _ec_jwk(key, kid: str) -> dict:
    numbers = key.public_key().public_numbers()
    return {
        "kty": "EC",
        "kid": kid,
        "use": "sig",
        "alg": "ES256",
        "crv": "P-256",
        "x": _b64url(numbers.x.to_bytes(32, "big")),
        "y": _b64url(numbers.y.to_bytes(32, "big")),
    }


@pytest.fixture(scope="module")
def jwks(rsa_key, ec_key):
    return {"keys": [_rsa_jwk(rsa_key, RSA_KID), _ec_jwk(ec_key, EC_KID)]}


def _mint(key, kid, alg, claims: dict, raw_sig: bytes | None = None) -> str:
    """Mint a test token: the header is labelled `alg` verbatim while the
    signature is always made with the key's own primitive, so unexpected-alg
    vectors exercise header validation rather than the minter."""
    header = {"typ": "JWT", "alg": alg, "kid": kid}
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}"
    if raw_sig is not None:
        signature = raw_sig
    elif isinstance(key, ec.EllipticCurvePrivateKey):
        r, s = decode_dss_signature(
            key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
        )
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    else:
        signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64url(signature)}"


def _claims(**over) -> dict:
    base = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "exp": NOW + 300,
        "iat": NOW - 10,
        "nonce": NONCE,
        "sub": "user-uuid-1",
        "email": "johlan@example.com",
        "email_verified": True,
        "preferred_username": "johlan",
    }
    base.update(over)
    return base


def _check(token, jwks, **kw) -> dict:
    return validate_id_token(
        token, jwks=jwks, issuer=ISSUER, client_id=CLIENT_ID, nonce=NONCE, now=NOW, **kw
    )


def test_valid_rsa_token(jwks, rsa_key):
    claims = _check(_mint(rsa_key, RSA_KID, "RS256", _claims()), jwks)
    assert claims["sub"] == "user-uuid-1"


def test_valid_ec_token(jwks, ec_key):
    claims = _check(_mint(ec_key, EC_KID, "ES256", _claims()), jwks)
    assert claims["sub"] == "user-uuid-1"


def test_tampered_payload_rejected(jwks, rsa_key):
    token = _mint(rsa_key, RSA_KID, "RS256", _claims())
    head, payload, sig = token.split(".")
    tampered = json.loads(base64.urlsafe_b64decode(payload + "=="))
    tampered["sub"] = "attacker"
    forged = f"{head}.{_b64url(json.dumps(tampered).encode())}.{sig}"
    with pytest.raises(OidcError):
        _check(forged, jwks)


@pytest.mark.parametrize("alg", ["none", "None", "NONE", "", "HS256", "RS512"])
def test_unexpected_alg_rejected(jwks, rsa_key, alg):
    token = _mint(rsa_key, RSA_KID, alg, _claims())
    with pytest.raises(OidcError):
        _check(token, jwks)


def test_alg_confusion_hs256_against_rsa_key(jwks):
    """A token claiming HS256 must not verify against an RSA JWKS entry,
    even if the HMAC happens to check out under some reading."""
    import hmac

    header = {"typ": "JWT", "alg": "HS256", "kid": RSA_KID}
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(_claims()).encode())}"
    mac = hmac.new(b"attacker-secret", signing_input.encode(), "sha256").digest()
    with pytest.raises(OidcError):
        _check(f"{signing_input}.{_b64url(mac)}", jwks)


def test_alg_confusion_rs256_against_ec_key(jwks, rsa_key):
    """An RS256 signature must not verify against the EC key, even when the
    kid names it."""
    token = _mint(rsa_key, EC_KID, "RS256", _claims())
    with pytest.raises(OidcError):
        _check(token, jwks)


def test_missing_and_unknown_kid_rejected(jwks, rsa_key):
    token = _mint(rsa_key, RSA_KID, "RS256", _claims())
    head, payload, sig = token.split(".")
    header = json.loads(base64.urlsafe_b64decode(head + "=="))
    for bad_kid in (None, "", "no-such-key"):
        header["kid"] = bad_kid
        forged = f"{_b64url(json.dumps(header).encode())}.{payload}.{sig}"
        with pytest.raises(OidcError):
            _check(forged, jwks)


def test_first_key_never_defaulted(jwks, rsa_key, ec_key):
    """With a kid naming no key, validation fails even though the JWKS holds
    keys that could verify the token under a laxer lookup."""
    single = {"keys": [jwks["keys"][1]]}  # EC only
    token = _mint(rsa_key, RSA_KID, "RS256", _claims())
    with pytest.raises(OidcError):
        _check(token, single)


def test_signature_by_other_key_in_same_jwks_rejected(jwks, rsa_key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _mint(other, RSA_KID, "RS256", _claims())
    with pytest.raises(OidcError):
        _check(token, jwks)


def test_issuer_mismatch_rejected(jwks, rsa_key):
    token = _mint(rsa_key, RSA_KID, "RS256", _claims(iss="https://evil.example.com/"))
    with pytest.raises(OidcError):
        _check(token, jwks)


def test_issuer_trailing_slash_tolerated_symmetrically(jwks, rsa_key):
    """One trailing slash is insignificant, whichever side carries it (real
    providers vary); anything beyond that still mismatches."""
    assert ISSUER.endswith("/")
    noslash = ISSUER[:-1]
    slashed = _mint(rsa_key, RSA_KID, "RS256", _claims(iss=ISSUER))
    assert (
        validate_id_token(
            slashed, jwks=jwks, issuer=noslash, client_id=CLIENT_ID, nonce=NONCE, now=NOW
        )["sub"]
        == "user-uuid-1"
    )
    bare = _mint(rsa_key, RSA_KID, "RS256", _claims(iss=noslash))
    assert _check(bare, jwks)["sub"] == "user-uuid-1"
    for bad in (
        "https://auth.example.com/application/o/other/",
        "https://auth.example.com/application/o/other",
        "https://evil.example.com/application/o/cadence/",
        None,
    ):
        with pytest.raises(OidcError):
            _check(_mint(rsa_key, RSA_KID, "RS256", _claims(iss=bad)), jwks)


def test_audience_string_and_array(jwks, rsa_key):
    assert _check(_mint(rsa_key, RSA_KID, "RS256", _claims(aud=[CLIENT_ID, "other"])), jwks)
    for bad_aud in ("someone-else", ["someone-else"], [], None):
        token = _mint(rsa_key, RSA_KID, "RS256", _claims(aud=bad_aud))
        with pytest.raises(OidcError):
            _check(token, jwks)


def test_expired_and_premature_rejected(jwks, rsa_key):
    expired = _mint(rsa_key, RSA_KID, "RS256", _claims(exp=NOW - 200))
    with pytest.raises(OidcError):
        _check(expired, jwks)
    premature = _mint(rsa_key, RSA_KID, "RS256", _claims(iat=NOW + 500))
    with pytest.raises(OidcError):
        _check(premature, jwks)
    # Inside the skew window is fine.
    edge = _mint(rsa_key, RSA_KID, "RS256", _claims(exp=NOW - 60))
    assert _check(edge, jwks)["sub"] == "user-uuid-1"


def test_nonce_mismatch_rejected(jwks, rsa_key):
    token = _mint(rsa_key, RSA_KID, "RS256", _claims(nonce="other-nonce"))
    with pytest.raises(OidcError):
        _check(token, jwks)
    with pytest.raises(OidcError):
        validate_id_token(
            _mint(rsa_key, RSA_KID, "RS256", _claims()),
            jwks=jwks,
            issuer=ISSUER,
            client_id=CLIENT_ID,
            nonce="other-nonce",
            now=NOW,
        )


def test_malformed_tokens_rejected(jwks):
    for bad in ("", "a.b", "a.b.c.d", "!!!.@@@.###"):
        with pytest.raises(OidcError):
            _check(bad, jwks)


def test_select_jwk_requires_exact_kid_and_alg(jwks):
    assert select_jwk(jwks, RSA_KID, "RS256")["kty"] == "RSA"
    for kid, alg in ((None, "RS256"), ("", "RS256"), ("nope", "RS256"), (RSA_KID, "none")):
        with pytest.raises(OidcError):
            select_jwk(jwks, kid, alg)


def test_actor_prefers_verified_email(jwks):
    assert actor_from_claims(_claims()) == "johlan@example.com"
    assert actor_from_claims(_claims(email_verified=False)) == "johlan"
    assert (
        actor_from_claims({"sub": "abc", "preferred_username": ""}) == "abc"
    )
    assert actor_from_claims({"sub": "abc"}) == "abc"


FERNET_KEY = Fernet.generate_key().decode()


def test_session_roundtrip_and_expiry():
    token = seal_session(
        FERNET_KEY, sub="user-uuid-1", email="a@b.c", name=None, ttl_seconds=3600
    )
    opened = open_session(FERNET_KEY, token)
    assert opened is not None and opened["sub"] == "user-uuid-1"
    assert opened["v"] == "cadence-session-v1"
    assert open_session(FERNET_KEY, token + "x") is None
    swapped = token[:10] + ("A" if token[10] != "A" else "B") + token[11:]
    assert open_session(FERNET_KEY, swapped) is None
    expired = seal_session(
        FERNET_KEY, sub="user-uuid-1", email=None, name=None, ttl_seconds=-1
    )
    assert open_session(FERNET_KEY, expired) is None


def test_session_rejects_foreign_tokens():
    agent_like = Fernet(FERNET_KEY.encode()).encrypt(b'{"kind": "agent"}').decode()
    assert open_session(FERNET_KEY, agent_like) is None
    assert open_session(FERNET_KEY, "not-a-token") is None
    other_key = Fernet.generate_key().decode()
    token = seal_session(other_key, sub="u", email=None, name=None, ttl_seconds=60)
    assert open_session(FERNET_KEY, token) is None


def test_parse_compact_rejects_non_jws():
    with pytest.raises(OidcError):
        parse_compact("just-a-string")
    with pytest.raises(OidcError):
        _check("e30.e30.e30", {"keys": []})


def test_fetch_json_wraps_transport_failures(monkeypatch):
    """DNS/refused/timeout/HTTP-error from the provider surface as
    OidcError (the routes turn it into 502/401), never raw."""
    failures = [
        urllib.error.URLError("dns down"),
        urllib.error.HTTPError("https://p/", 404, "Not Found", {}, None),
        socket.timeout("timed out"),
        ConnectionRefusedError("refused"),
    ]
    for failure in failures:
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda *a, _f=failure, **k: (_ for _ in ()).throw(_f),
        )
        with pytest.raises(OidcError):
            fetch_json("https://provider.example.com/.well-known/openid-configuration")


def test_exchange_code_wraps_read_timeout(monkeypatch):
    """A stall mid-body is a provider failure too, not a 500."""

    class SlowResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            raise socket.timeout("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: SlowResp())
    with pytest.raises(OidcError):
        exchange_code(
            "https://provider.example.com/token/",
            code="c",
            redirect_uri="https://cb",
            client_id="id",
            client_secret="s",
        )


def test_now_defaults_to_wall_clock(jwks, rsa_key):
    live = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "exp": int(time.time()) + 300,
        "iat": int(time.time()) - 5,
        "nonce": NONCE,
        "sub": "u",
    }
    claims = validate_id_token(
        _mint(rsa_key, RSA_KID, "RS256", live),
        jwks=jwks,
        issuer=ISSUER,
        client_id=CLIENT_ID,
        nonce=NONCE,
    )
    assert claims["sub"] == "u"
