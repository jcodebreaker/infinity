"""Sealing a small secret value under the server's vault key (AES-256-GCM).

Shared by the vault (saved logins) and the model-credential store (provider API keys). The
owner and secret id are bound in as associated data, so a sealed value only opens for its own
row. With no valid ``OMNIGENT_VAULT_KEY`` it fails closed.
"""

from __future__ import annotations

import base64
import binascii
import os
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VAULT_KEY_ENV = "OMNIGENT_VAULT_KEY"
_PREFIX = "v1:"


class VaultUnavailableError(RuntimeError):
    """The vault has no usable key (unset or malformed): it refuses to store or reveal."""


def require_key() -> bytes:
    raw = os.environ.get(VAULT_KEY_ENV, "").strip()
    if not raw:
        raise VaultUnavailableError(f"the vault is off: {VAULT_KEY_ENV} is not set")
    try:
        key = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_")
    except (binascii.Error, ValueError):
        key = b""
    if len(key) != 32:
        raise VaultUnavailableError(f"the vault is off: {VAULT_KEY_ENV} must be 32 bytes, base64")
    return key


def _aad(user_id: str | None, secret_id: str) -> bytes:
    return f"{user_id or ''}|{secret_id}".encode()


def seal(value: str, *, user_id: str | None, secret_id: str) -> str:
    """:returns: ``v1:`` + base64(nonce + ciphertext) of value."""
    nonce = secrets.token_bytes(12)
    blob = AESGCM(require_key()).encrypt(nonce, value.encode(), _aad(user_id, secret_id))
    return _PREFIX + base64.b64encode(nonce + blob).decode("ascii")


def unseal(token: str, *, user_id: str | None, secret_id: str) -> str:
    """:returns: The plaintext; raises :class:`VaultUnavailableError` on a wrong key or tamper."""
    key = require_key()
    try:
        raw = base64.b64decode(token.removeprefix(_PREFIX))
        return AESGCM(key).decrypt(raw[:12], raw[12:], _aad(user_id, secret_id)).decode()
    except (InvalidTag, ValueError, binascii.Error) as exc:
        raise VaultUnavailableError("the saved value cannot be read with this key") from exc
