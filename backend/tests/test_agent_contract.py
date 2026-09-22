"""Agent<->backend signature contract (docs/decisions.md "Authentication").

The golden vector in testdata/signed_request.json was signed by the real Go
signer (agent/internal/client Client.sign). These tests replay it through the
real Python verifier (app.api.deps._auth_signed), so a one-sided change --
renamed signed header, different canonical string, timestamp/nonce format
drift -- fails here even if each side's own suite still passes.

The golden timestamp is fixed at generation time; `now` is derived from it on
every run, so the vector never goes stale inside the 300 s anti-replay
window. Time itself is not under contract -- only the wire format.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException, status
from starlette.requests import Request

from app.api.deps import _auth_signed
from app.core.crypto import encrypt_token_secret
from tests.conftest import create_host
from tests.test_agent_tokens import _add_token

GOLDEN_PATH = pathlib.Path(__file__).resolve().parent / "testdata" / "signed_request.json"


def _golden() -> dict:
    return json.loads(GOLDEN_PATH.read_text())


def _request(*, method: str, path: str, body: bytes) -> Request:
    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "server": ("testserver", 80),
            "path": path,
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 40000),
        },
        receive,
    )


def _setup_golden_token(client, db_session, golden: dict) -> str:
    """Provision a host, then attach the golden test key as a second token.

    Returns the host id. Uses the prod crypto path (encrypt_token_secret) so
    only the signature bytes -- never the DB plumbing -- come from the file.
    """
    host_id, _token = create_host(client)
    _add_token(
        db_session,
        host_id,
        secret=golden["key"],
        secret_encrypted=encrypt_token_secret(golden["key"]),
        label="golden-contract",
    )
    return host_id


def test_golden_vector_is_accepted(client, db_session):
    golden = _golden()
    host_id = _setup_golden_token(client, db_session, golden)
    body = golden["body"].encode()
    now = datetime.fromtimestamp(int(golden["timestamp"]), tz=timezone.utc)
    host, tok = asyncio.run(
        _auth_signed(
            _request(method=golden["method"], path=golden["path"], body=body),
            db_session,
            "127.0.0.1",
            golden["token_hash"],
            golden["timestamp"],
            golden["signature"],
            now,
        )
    )
    assert str(host.id) == host_id
    assert tok.token_hash == golden["token_hash"]


def test_tampered_body_is_rejected(client, db_session):
    golden = _golden()
    _setup_golden_token(client, db_session, golden)
    body = bytearray(golden["body"].encode())
    body[0] ^= 1  # one byte changed after signing
    now = datetime.fromtimestamp(int(golden["timestamp"]), tz=timezone.utc)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            _auth_signed(
                _request(method=golden["method"], path=golden["path"], body=bytes(body)),
                db_session,
                "127.0.0.1",
                golden["token_hash"],
                golden["timestamp"],
                golden["signature"],
                now,
            )
        )
    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED


def test_tampered_method_is_rejected(client, db_session):
    golden = _golden()
    _setup_golden_token(client, db_session, golden)
    body = golden["body"].encode()
    assert golden["method"] == "POST"
    now = datetime.fromtimestamp(int(golden["timestamp"]), tz=timezone.utc)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            _auth_signed(
                _request(method="GET", path=golden["path"], body=body),
                db_session,
                "127.0.0.1",
                golden["token_hash"],
                golden["timestamp"],
                golden["signature"],
                now,
            )
        )
    assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
