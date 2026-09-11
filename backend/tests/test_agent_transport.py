from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.models import AgentCertificate
from tests.conftest import create_host, report_payload, signed

PROXY_KEY = "transport-test-key-that-is-long-enough"


def _headers(transport: str, fingerprint: str | None = None, key: str = PROXY_KEY) -> dict:
    headers = {
        "X-Cadence-Transport": transport,
        "X-Cadence-Proxy-Key": key,
    }
    if fingerprint is not None:
        headers["X-Cadence-Client-Cert-Fingerprint"] = fingerprint
    return headers


def _require_transport(monkeypatch, *, legacy: bool = True) -> None:
    monkeypatch.setattr("app.api.deps.settings.require_agent_transport_auth", True)
    monkeypatch.setattr("app.api.deps.settings.internal_proxy_key", PROXY_KEY)
    monkeypatch.setattr("app.api.deps.settings.legacy_agent_endpoints", legacy)


def test_legacy_transport_is_explicit_and_can_be_disabled(client, monkeypatch):
    host_id, token = create_host(client)
    _require_transport(monkeypatch)

    assert client.post(
        "/api/v1/reports", auth=signed(token), json=report_payload()
    ).status_code == 401
    assert client.post(
        "/api/v1/reports",
        auth=signed(token),
        headers=_headers("legacy", key="wrong-key-that-is-also-long-enough"),
        json=report_payload(),
    ).status_code == 401
    assert client.post(
        "/api/v1/reports",
        auth=signed(token),
        headers=_headers("legacy"),
        json=report_payload(),
    ).status_code == 200

    monkeypatch.setattr("app.api.deps.settings.legacy_agent_endpoints", False)
    assert client.post(
        "/api/v1/reports",
        auth=signed(token),
        headers=_headers("legacy"),
        json=report_payload(host_id=host_id),
    ).status_code == 401


def test_mtls_fingerprint_must_belong_to_hmac_host(
    client, db_session, monkeypatch
):
    host_id, token = create_host(client, hostname="vm-mtls")
    other_host_id, _ = create_host(client, hostname="vm-other")
    now = datetime.now(timezone.utc)
    good_fingerprint = "a" * 64
    other_fingerprint = "b" * 64
    db_session.add_all(
        [
            AgentCertificate(
                host_id=host_id,
                serial_number="a1",
                fingerprint_sha256=good_fingerprint,
                not_before=now - timedelta(minutes=1),
                expires_at=now + timedelta(days=1),
            ),
            AgentCertificate(
                host_id=other_host_id,
                serial_number="b1",
                fingerprint_sha256=other_fingerprint,
                not_before=now - timedelta(minutes=1),
                expires_at=now + timedelta(days=1),
            ),
        ]
    )
    db_session.commit()
    _require_transport(monkeypatch)

    assert client.post(
        "/api/v1/reports",
        auth=signed(token),
        headers=_headers("mtls", other_fingerprint),
        json=report_payload(),
    ).status_code == 401
    assert client.post(
        "/api/v1/reports",
        auth=signed(token),
        headers=_headers("mtls", good_fingerprint),
        json=report_payload(),
    ).status_code == 200

    db_session.expire_all()
    certificate = db_session.execute(
        select(AgentCertificate).where(
            AgentCertificate.fingerprint_sha256 == good_fingerprint
        )
    ).scalar_one()
    assert certificate.last_used_at is not None


def test_revoked_mtls_certificate_is_rejected(client, db_session, monkeypatch):
    host_id, token = create_host(client, hostname="vm-revoked")
    now = datetime.now(timezone.utc)
    fingerprint = "c" * 64
    db_session.add(
        AgentCertificate(
            host_id=host_id,
            serial_number="c1",
            fingerprint_sha256=fingerprint,
            not_before=now - timedelta(minutes=1),
            expires_at=now + timedelta(days=1),
            revoked_at=now,
        )
    )
    db_session.commit()
    _require_transport(monkeypatch)

    assert client.post(
        "/api/v1/reports",
        auth=signed(token),
        headers=_headers("mtls", fingerprint),
        json=report_payload(),
    ).status_code == 401
