from __future__ import annotations

import stat

import pytest
from cryptography import x509
from cryptography.x509.oid import ExtensionOID

from app.pki.client_ca import ensure_client_ca


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

