"""Enrollment-code generation and parsing primitives."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509

_CODE_RE = re.compile(r"^cad1\.([A-Za-z0-9_-]{32})\.([0-9a-f]{64})$")


@dataclass(frozen=True)
class EnrollmentCodeParts:
    secret: str
    secret_hash: str
    ca_fingerprint_sha256: str


def server_ca_fingerprint(path: str | Path) -> str:
    """Hash the exact CA file bytes agents download and pin."""
    ca_path = Path(path)
    try:
        data = ca_path.read_bytes()
        x509.load_pem_x509_certificate(data)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read a valid server CA certificate from {ca_path}") from exc
    return hashlib.sha256(data).hexdigest()


def generate_enrollment_code(ca_file: str | Path) -> tuple[str, EnrollmentCodeParts]:
    secret = secrets.token_urlsafe(24)
    fingerprint = server_ca_fingerprint(ca_file)
    parts = EnrollmentCodeParts(
        secret=secret,
        secret_hash=hashlib.sha256(secret.encode()).hexdigest(),
        ca_fingerprint_sha256=fingerprint,
    )
    return f"cad1.{secret}.{fingerprint}", parts


def parse_enrollment_code(code: str) -> EnrollmentCodeParts:
    match = _CODE_RE.fullmatch(code.strip())
    if match is None:
        raise ValueError("invalid enrollment code")
    secret, fingerprint = match.groups()
    return EnrollmentCodeParts(
        secret=secret,
        secret_hash=hashlib.sha256(secret.encode()).hexdigest(),
        ca_fingerprint_sha256=fingerprint,
    )
