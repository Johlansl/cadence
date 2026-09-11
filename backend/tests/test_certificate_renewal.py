from __future__ import annotations

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlalchemy import select

from app.models.models import AgentCertificate, AuditLog
from app.pki.client_ca import ensure_client_ca
from tests.conftest import ADMIN_HEADERS, signed

PROXY_KEY = "transport-test-key-that-is-long-enough"


def _csr() -> tuple[ec.EllipticCurvePrivateKey, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agent")]))
        .sign(key, hashes.SHA256())
    )
    return key, csr.public_bytes(serialization.Encoding.PEM).decode()


def _enroll(client, tmp_path, monkeypatch) -> dict:
    server = ensure_client_ca(tmp_path / "server")
    monkeypatch.setattr(
        "app.api.routes.enrollments.settings.server_ca_file", str(server.root_certificate)
    )
    monkeypatch.setattr(
        "app.api.routes.enrollments.settings.client_pki_dir", str(tmp_path / "client")
    )
    created = client.post(
        "/api/v1/admin/enrollments",
        headers=ADMIN_HEADERS,
        json={"expected_hostname": "vm-renew"},
    ).json()
    _, csr = _csr()
    response = client.post(
        "/api/v1/agent/enroll",
        json={"code": created["code"], "hostname": "vm-renew", "csr_pem": csr},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _transport_headers(fingerprint: str, transport: str = "mtls") -> dict[str, str]:
    return {
        "X-Cadence-Transport": transport,
        "X-Cadence-Proxy-Key": PROXY_KEY,
        "X-Cadence-Client-Cert-Fingerprint": fingerprint,
    }


def test_mtls_hmac_renewal_keeps_old_certificate_active(
    client, db_session, tmp_path, monkeypatch
):
    enrolled = _enroll(client, tmp_path, monkeypatch)
    old_leaf = x509.load_pem_x509_certificate(
        enrolled["client_certificate_pem"].encode()
    )
    old_fingerprint = old_leaf.fingerprint(hashes.SHA256()).hex()
    new_key, csr = _csr()
    monkeypatch.setattr("app.api.deps.settings.require_agent_transport_auth", True)
    monkeypatch.setattr("app.api.deps.settings.internal_proxy_key", PROXY_KEY)

    response = client.post(
        "/api/v1/agent/certificate/renew",
        auth=signed(enrolled["token"]),
        headers=_transport_headers(old_fingerprint),
        json={"csr_pem": csr},
    )
    assert response.status_code == 200, response.text
    renewed = response.json()
    new_leaf = x509.load_pem_x509_certificate(
        renewed["client_certificate_pem"].encode()
    )
    assert new_leaf.public_key().public_numbers() == new_key.public_key().public_numbers()
    assert renewed["fingerprint_sha256"] == new_leaf.fingerprint(hashes.SHA256()).hex()

    rows = db_session.execute(
        select(AgentCertificate).where(AgentCertificate.host_id == enrolled["host_id"])
    ).scalars().all()
    assert len(rows) == 2
    assert all(row.revoked_at is None for row in rows)
    assert {row.fingerprint_sha256 for row in rows} == {
        old_fingerprint,
        renewed["fingerprint_sha256"],
    }
    audit = db_session.execute(
        select(AuditLog).where(AuditLog.action == "certificate.renew")
    ).scalar_one()
    assert audit.detail["host_id"] == enrolled["host_id"]


def test_renewal_rejects_legacy_transport(client, tmp_path, monkeypatch):
    enrolled = _enroll(client, tmp_path, monkeypatch)
    leaf = x509.load_pem_x509_certificate(enrolled["client_certificate_pem"].encode())
    fingerprint = leaf.fingerprint(hashes.SHA256()).hex()
    _, csr = _csr()
    monkeypatch.setattr("app.api.deps.settings.require_agent_transport_auth", True)
    monkeypatch.setattr("app.api.deps.settings.internal_proxy_key", PROXY_KEY)
    monkeypatch.setattr("app.api.deps.settings.legacy_agent_endpoints", True)

    response = client.post(
        "/api/v1/agent/certificate/renew",
        auth=signed(enrolled["token"]),
        headers=_transport_headers(fingerprint, transport="legacy"),
        json={"csr_pem": csr},
    )
    assert response.status_code == 401


def test_admin_lists_and_revokes_certificate(client, db_session, tmp_path, monkeypatch):
    enrolled = _enroll(client, tmp_path, monkeypatch)
    response = client.get(
        f"/api/v1/admin/hosts/{enrolled['host_id']}/certificates",
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200
    listed = response.json()
    assert len(listed) == 1 and listed[0]["state"] == "active"
    certificate_id = listed[0]["id"]

    assert client.delete(
        f"/api/v1/admin/hosts/{enrolled['host_id']}/certificates/{certificate_id}",
        headers=ADMIN_HEADERS,
    ).status_code == 204
    assert client.delete(
        f"/api/v1/admin/hosts/{enrolled['host_id']}/certificates/{certificate_id}",
        headers=ADMIN_HEADERS,
    ).status_code == 204
    db_session.expire_all()
    assert db_session.get(AgentCertificate, certificate_id).revoked_at is not None
