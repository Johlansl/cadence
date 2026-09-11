"""Private PKI used to authenticate Cadence agent TLS clients.

The server CA used by Caddy and pinned during enrollment is intentionally a
different trust domain. This module owns only the client certificates accepted
on the dedicated agent listener.
"""

from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ROOT_CERT_FILENAME = "client-root-ca.crt"
ROOT_KEY_FILENAME = "client-root-ca.key"
INTERMEDIATE_CERT_FILENAME = "client-intermediate-ca.crt"
INTERMEDIATE_KEY_FILENAME = "client-intermediate-ca.key"

_ROOT_LIFETIME = timedelta(days=3650)
_INTERMEDIATE_LIFETIME = timedelta(days=1825)
_CLOCK_SKEW = timedelta(minutes=5)


@dataclass(frozen=True)
class ClientCAPaths:
    directory: Path
    root_certificate: Path
    root_private_key: Path
    intermediate_certificate: Path
    intermediate_private_key: Path


def paths(directory: str | Path) -> ClientCAPaths:
    base = Path(directory)
    return ClientCAPaths(
        directory=base,
        root_certificate=base / ROOT_CERT_FILENAME,
        root_private_key=base / ROOT_KEY_FILENAME,
        intermediate_certificate=base / INTERMEDIATE_CERT_FILENAME,
        intermediate_private_key=base / INTERMEDIATE_KEY_FILENAME,
    )


def _name(common_name: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Cadence"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


def _write_file(path: Path, data: bytes, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _certificate_bytes(certificate: x509.Certificate) -> bytes:
    return certificate.public_bytes(serialization.Encoding.PEM)


def _private_key_bytes(private_key: ec.EllipticCurvePrivateKey) -> bytes:
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _create_material(target: ClientCAPaths) -> None:
    now = datetime.now(timezone.utc)
    root_key = ec.generate_private_key(ec.SECP256R1())
    root_subject = _name("Cadence client root CA")
    root_cert = (
        x509.CertificateBuilder()
        .subject_name(root_subject)
        .issuer_name(root_subject)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _CLOCK_SKEW)
        .not_valid_after(now + _ROOT_LIFETIME)
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), False)
        .sign(root_key, hashes.SHA256())
    )

    intermediate_key = ec.generate_private_key(ec.SECP256R1())
    intermediate_subject = _name("Cadence client intermediate CA")
    intermediate_cert = (
        x509.CertificateBuilder()
        .subject_name(intermediate_subject)
        .issuer_name(root_cert.subject)
        .public_key(intermediate_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _CLOCK_SKEW)
        .not_valid_after(now + _INTERMEDIATE_LIFETIME)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(intermediate_key.public_key()), False
        )
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), False)
        .sign(root_key, hashes.SHA256())
    )

    _write_file(target.root_private_key, _private_key_bytes(root_key), 0o600)
    _write_file(target.root_certificate, _certificate_bytes(root_cert), 0o644)
    _write_file(target.intermediate_private_key, _private_key_bytes(intermediate_key), 0o600)
    _write_file(target.intermediate_certificate, _certificate_bytes(intermediate_cert), 0o644)


def _load_and_validate(target: ClientCAPaths) -> None:
    try:
        root_cert = x509.load_pem_x509_certificate(target.root_certificate.read_bytes())
        root_key = serialization.load_pem_private_key(
            target.root_private_key.read_bytes(), password=None
        )
        intermediate_cert = x509.load_pem_x509_certificate(
            target.intermediate_certificate.read_bytes()
        )
        intermediate_key = serialization.load_pem_private_key(
            target.intermediate_private_key.read_bytes(), password=None
        )
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"invalid client PKI material in {target.directory}") from exc

    if not isinstance(root_key, ec.EllipticCurvePrivateKey) or not isinstance(
        intermediate_key, ec.EllipticCurvePrivateKey
    ):
        raise RuntimeError("client PKI private keys must be ECDSA keys")
    if root_key.public_key().public_numbers() != root_cert.public_key().public_numbers():
        raise RuntimeError("client root CA certificate does not match its private key")
    if (
        intermediate_key.public_key().public_numbers()
        != intermediate_cert.public_key().public_numbers()
    ):
        raise RuntimeError("client intermediate CA certificate does not match its private key")
    if intermediate_cert.issuer != root_cert.subject:
        raise RuntimeError("client intermediate CA was not issued by the configured root CA")
    try:
        root_cert.public_key().verify(
            intermediate_cert.signature,
            intermediate_cert.tbs_certificate_bytes,
            ec.ECDSA(intermediate_cert.signature_hash_algorithm),
        )
    except Exception as exc:
        raise RuntimeError("client intermediate CA signature is invalid") from exc


def ensure_client_ca(directory: str | Path) -> ClientCAPaths:
    """Create the client root and intermediate once, then validate them.

    A process lock serializes initialization. Partial material is never
    repaired by silently rotating the CA: operators must restore or remove the
    isolated client-PKI volume deliberately.
    """
    target = paths(directory)
    target.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target.directory, 0o700)
    lock_path = target.directory / ".init.lock"
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        material_paths = (
            target.root_certificate,
            target.root_private_key,
            target.intermediate_certificate,
            target.intermediate_private_key,
        )
        present = [item.exists() for item in material_paths]
        if any(present) and not all(present):
            raise RuntimeError(
                f"incomplete client PKI material in {target.directory}; refusing CA rotation"
            )
        if not any(present):
            _create_material(target)
        _load_and_validate(target)
    return target

