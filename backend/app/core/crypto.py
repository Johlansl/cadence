"""Encrypt / decrypt agent_tokens.secret_encrypted at rest.

A signed request (see app.api.deps) needs the server to recompute an HMAC,
which needs the real secret -- a one-way hash like token_hash is not enough.
MultiFernet always encrypts with the first (newest) key and can decrypt with
any key in the list, the same rotation shape CADENCE_ADMIN_KEY /
CADENCE_ADMIN_KEY_PREVIOUS already uses.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import settings

_keys = [Fernet(settings.token_encryption_key.encode())]
if settings.token_encryption_key_previous:
    _keys.append(Fernet(settings.token_encryption_key_previous.encode()))
_fernet = MultiFernet(_keys)


def encrypt_token_secret(secret: str) -> str:
    """Encrypt a plaintext token secret for storage. Always uses the primary
    (first) key."""
    return _fernet.encrypt(secret.encode()).decode()


def decrypt_token_secret(ciphertext: str) -> str | None:
    """Decrypt a stored token secret. None if it does not verify against any
    configured key (wrong/rotated-away key, or corrupt data) -- callers treat
    that the same as "cannot verify this token's signature"."""
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return None
