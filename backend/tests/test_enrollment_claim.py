from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID, NameOID
from sqlalchemy import func, select

from app.models.models import AgentCertificate, AgentToken, EnrollmentCode, Host
from app.pki.client_ca import ensure_client_ca
from tests.conftest import ADMIN_HEADERS, create_host, report_payload, signed


def _configure_pki(tmp_path, monkeypatch):
    server = ensure_client_ca(tmp_path / "server")
    monkeypatch.setattr(
        "app.api.routes.enrollments.settings.server_ca_file", str(server.root_certificate)
    )
    monkeypatch.setattr(
        "app.api.routes.enrollments.settings.client_pki_dir", str(tmp_path / "client")
    )


def _csr() -> tuple[ec.EllipticCurvePrivateKey, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    request = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agent")]))
        .sign(key, hashes.SHA256())
    )
    return key, request.public_bytes(serialization.Encoding.PEM).decode()


def _new_code(client, hostname: str) -> dict:
    response = client.post(
        "/api/v1/admin/enrollments",
        headers=ADMIN_HEADERS,
        json={"expected_hostname": hostname, "tags": {"role": "test"}},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_claim_creates_host_hmac_token_and_client_certificate_once(
    client, db_session, tmp_path, monkeypatch
):
    _configure_pki(tmp_path, monkeypatch)
    created = _new_code(client, "vm-enrolled")
    key, csr = _csr()

    response = client.post(
        "/api/v1/agent/enroll",
        json={"code": created["code"], "hostname": "vm-enrolled", "csr_pem": csr},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["server_url"] == "https://cadence.lan:8443"

    host = db_session.get(Host, body["host_id"])
    assert host.hostname == "vm-enrolled"
    assert host.tags == {"role": "test"}
    enrollment = db_session.get(EnrollmentCode, created["id"])
    assert enrollment.consumed_at is not None
    assert str(enrollment.enrolled_host_id) == body["host_id"]
    assert db_session.execute(
        select(func.count()).select_from(AgentToken).where(AgentToken.host_id == host.id)
    ).scalar_one() == 1

    certificate_row = db_session.execute(
        select(AgentCertificate).where(AgentCertificate.host_id == host.id)
    ).scalar_one()
    leaf = x509.load_pem_x509_certificate(body["client_certificate_pem"].encode())
    assert certificate_row.fingerprint_sha256 == leaf.fingerprint(hashes.SHA256()).hex()
    assert leaf.public_key().public_numbers() == key.public_key().public_numbers()
    assert leaf.extensions.get_extension_for_oid(ExtensionOID.EXTENDED_KEY_USAGE).value == (
        x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH])
    )

    second = client.post(
        "/api/v1/agent/enroll",
        json={"code": created["code"], "hostname": "vm-enrolled", "csr_pem": csr},
    )
    assert second.status_code == 401
    assert db_session.execute(
        select(func.count()).select_from(Host).where(Host.hostname == "vm-enrolled")
    ).scalar_one() == 1
    assert client.post(
        "/api/v1/reports", auth=signed(body["token"]), json=report_payload()
    ).status_code == 200


def test_existing_host_claim_preserves_legacy_token(
    client, db_session, tmp_path, monkeypatch
):
    _configure_pki(tmp_path, monkeypatch)
    host_id, legacy_token = create_host(client, hostname="vm-existing")
    created = client.post(
        "/api/v1/admin/enrollments",
        headers=ADMIN_HEADERS,
        json={"target_host_id": host_id},
    ).json()
    _, csr = _csr()
    claimed = client.post(
        "/api/v1/agent/enroll",
        json={"code": created["code"], "hostname": "vm-existing", "csr_pem": csr},
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["host_id"] == host_id
    assert db_session.execute(
        select(func.count()).select_from(AgentToken).where(AgentToken.host_id == host_id)
    ).scalar_one() == 2
    for token in (legacy_token, claimed.json()["token"]):
        assert client.post(
            "/api/v1/reports", auth=signed(token), json=report_payload()
        ).status_code == 200


def test_wrong_fingerprint_hostname_and_expiry_are_rejected(
    client, db_session, tmp_path, monkeypatch
):
    _configure_pki(tmp_path, monkeypatch)
    created = _new_code(client, "vm-enrolled")
    _, csr = _csr()

    prefix, secret, fingerprint = created["code"].split(".")
    replacement = "0" if fingerprint[-1] != "0" else "1"
    wrong_fingerprint = f"{prefix}.{secret}.{fingerprint[:-1]}{replacement}"
    for code, hostname in (
        (wrong_fingerprint, "vm-enrolled"),
        (created["code"], "another-host"),
    ):
        response = client.post(
            "/api/v1/agent/enroll",
            json={"code": code, "hostname": hostname, "csr_pem": csr},
        )
        assert response.status_code == 401

    row = db_session.get(EnrollmentCode, created["id"])
    row.expires_at = row.created_at + timedelta(microseconds=1)
    assert row.expires_at < datetime.now(timezone.utc)
    db_session.commit()
    response = client.post(
        "/api/v1/agent/enroll",
        json={"code": created["code"], "hostname": "vm-enrolled", "csr_pem": csr},
    )
    assert response.status_code == 401
    assert row.consumed_at is None


def test_bad_csr_does_not_consume_valid_code(client, db_session, tmp_path, monkeypatch):
    _configure_pki(tmp_path, monkeypatch)
    created = _new_code(client, "vm-enrolled")
    response = client.post(
        "/api/v1/agent/enroll",
        json={"code": created["code"], "hostname": "vm-enrolled", "csr_pem": "nope"},
    )
    assert response.status_code == 400
    db_session.expire_all()
    assert db_session.get(EnrollmentCode, created["id"]).consumed_at is None


def test_claim_requires_authenticated_enrollment_proxy(
    client, tmp_path, monkeypatch
):
    _configure_pki(tmp_path, monkeypatch)
    created = _new_code(client, "vm-enrolled")
    _, csr = _csr()
    monkeypatch.setattr(
        "app.api.routes.enrollments.settings.require_agent_transport_auth", True
    )
    monkeypatch.setattr(
        "app.api.routes.enrollments.settings.internal_proxy_key",
        "transport-test-key-that-is-long-enough",
    )

    payload = {
        "code": created["code"],
        "hostname": "vm-enrolled",
        "csr_pem": csr,
    }
    assert client.post("/api/v1/agent/enroll", json=payload).status_code == 401
    response = client.post(
        "/api/v1/agent/enroll",
        headers={
            "X-Cadence-Transport": "enrollment",
            "X-Cadence-Proxy-Key": "transport-test-key-that-is-long-enough",
        },
        json=payload,
    )
    assert response.status_code == 200, response.text
