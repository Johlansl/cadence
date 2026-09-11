from __future__ import annotations

import stat
import uuid

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtensionOID, NameOID

from app.pki.client_ca import ensure_client_ca, issue_client_certificate


def test_client_ca_is_created_once_with_constrained_intermediate(tmp_path):
    first = ensure_client_ca(tmp_path / "pki")
    root_bytes = first.root_certificate.read_bytes()
    intermediate_bytes = first.intermediate_certificate.read_bytes()

    second = ensure_client_ca(tmp_path / "pki")

    assert second == first
    assert second.root_certificate.read_bytes() == root_bytes
    assert second.intermediate_certificate.read_bytes() == intermediate_bytes
    assert stat.S_IMODE(second.root_private_key.stat().st_mode) == 0o600
    assert stat.S_IMODE(second.intermediate_private_key.stat().st_mode) == 0o600

    root = x509.load_pem_x509_certificate(root_bytes)
    intermediate = x509.load_pem_x509_certificate(intermediate_bytes)
    assert root.extensions.get_extension_for_oid(
        ExtensionOID.BASIC_CONSTRAINTS
    ).value.path_length == 1
    assert intermediate.extensions.get_extension_for_oid(
        ExtensionOID.BASIC_CONSTRAINTS
    ).value.path_length == 0
    assert intermediate.issuer == root.subject


def test_client_ca_refuses_partial_material(tmp_path):
    directory = tmp_path / "pki"
    material = ensure_client_ca(directory)
    material.intermediate_private_key.unlink()

    with pytest.raises(RuntimeError, match="incomplete client PKI material"):
        ensure_client_ca(directory)


def test_issue_client_certificate_binds_host_and_csr_key(tmp_path):
    private_key = ec.generate_private_key(ec.SECP256R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agent")]))
        .sign(private_key, hashes.SHA256())
    )
    host_id = uuid.uuid4()

    issued = issue_client_certificate(
        tmp_path / "pki",
        csr.public_bytes(serialization.Encoding.PEM).decode(),
        host_id,
    )

    leaf = x509.load_pem_x509_certificate(issued.certificate_chain_pem.encode())
    assert leaf.public_key().public_numbers() == private_key.public_key().public_numbers()
    san = leaf.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
    assert san.get_values_for_type(x509.UniformResourceIdentifier) == [
        f"urn:cadence:host:{host_id}"
    ]
    usages = leaf.extensions.get_extension_for_oid(ExtensionOID.EXTENDED_KEY_USAGE).value
    assert usages == x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH])
    assert issued.fingerprint_sha256 == leaf.fingerprint(hashes.SHA256()).hex()


def test_issue_client_certificate_rejects_non_p256_key(tmp_path):
    private_key = ec.generate_private_key(ec.SECP384R1())
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agent")]))
        .sign(private_key, hashes.SHA256())
    )
    with pytest.raises(ValueError, match="ECDSA P-256"):
        issue_client_certificate(
            tmp_path / "pki",
            csr.public_bytes(serialization.Encoding.PEM).decode(),
            uuid.uuid4(),
        )
