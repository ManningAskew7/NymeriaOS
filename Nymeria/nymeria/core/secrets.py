"""
Symmetric encryption for at-rest secrets (BYO Telegram bot tokens, future
OAuth refresh tokens, per-user integration API keys, ...).

Design:
- One Fernet key per deployment, stored in the ``NYMERIA_SECRETS_KEY`` env
  var. Never on disk next to the encrypted data.
- ``cryptography.fernet.Fernet`` is AES-128-CBC + HMAC-SHA256 with a
  built-in version byte and timestamp. Tokens come out as URL-safe base64
  strings safe to drop straight into a TEXT column.
- The key is read lazily on first use, not at import time, so the rest of
  the app keeps booting if BYO features aren't being used. The first call
  to ``encrypt`` or ``decrypt`` without a configured key raises
  ``SecretsKeyMissing`` with instructions on how to mint one.
- Generate a fresh key with ``Fernet.generate_key()`` (44 chars, ends in
  ``=``). Rotation is non-trivial — rotating the key means re-encrypting
  every stored value — so treat the key as permanent and back it up
  alongside (but separate from) the database.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


SECRETS_KEY_ENV_VAR = "NYMERIA_SECRETS_KEY"


class SecretsKeyMissing(RuntimeError):
    """Raised when an encrypt/decrypt call is made but the env var isn't set.

    The caller should surface a clear "set NYMERIA_SECRETS_KEY in .env.docker
    to enable this feature" message — see the BYO Telegram bot endpoints
    for the canonical handling.
    """


class SecretsKeyInvalid(RuntimeError):
    """``NYMERIA_SECRETS_KEY`` is set but isn't a valid Fernet key."""


_cipher: Optional[Fernet] = None
_loaded_for_key: Optional[str] = None


def _load_cipher() -> Fernet:
    """Lazy singleton — re-builds the Fernet instance if the env var changed
    (relevant for tests; production sets it once and never touches it).
    """
    global _cipher, _loaded_for_key
    raw = os.environ.get(SECRETS_KEY_ENV_VAR)
    if not raw:
        raise SecretsKeyMissing(
            f"{SECRETS_KEY_ENV_VAR} is not set. Generate a key with "
            f"`docker exec nymeria-api python3 -c \"from cryptography.fernet "
            f"import Fernet; print(Fernet.generate_key().decode())\"` and add "
            f"it to .env.docker. The key is permanent — rotation requires "
            f"re-encrypting every stored secret."
        )
    if _cipher is not None and _loaded_for_key == raw:
        return _cipher
    try:
        _cipher = Fernet(raw.encode("ascii"))
    except (ValueError, TypeError) as e:
        raise SecretsKeyInvalid(
            f"{SECRETS_KEY_ENV_VAR} is set but isn't a valid Fernet key. "
            f"Expected a 32-byte url-safe base64 string (44 chars ending in "
            f"'='). Generate one with Fernet.generate_key()."
        ) from e
    _loaded_for_key = raw
    return _cipher


def has_secrets_key() -> bool:
    """True if a key is configured. Use this to gate UI/API features that
    depend on at-rest encryption — e.g. the "Use my own bot" wizard should
    refuse cleanly with a 503 when this returns False.
    """
    return bool(os.environ.get(SECRETS_KEY_ENV_VAR))


def encrypt(plaintext: str) -> str:
    """Encrypt a UTF-8 string. Returns a Fernet token (url-safe base64).

    Raises ``SecretsKeyMissing`` if the env var isn't set, or
    ``SecretsKeyInvalid`` if it's malformed. Both cases should bubble up
    as a 503 with a helpful message.
    """
    if not isinstance(plaintext, str):
        raise TypeError("encrypt() requires a str")
    cipher = _load_cipher()
    return cipher.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Decrypt a Fernet token produced by ``encrypt``. Returns the plaintext.

    Raises ``SecretsKeyMissing`` / ``SecretsKeyInvalid`` for env-var problems,
    or ``cryptography.fernet.InvalidToken`` if the ciphertext is corrupted
    or was encrypted with a different key (e.g. after an unintended key
    rotation).
    """
    if not isinstance(ciphertext, str):
        raise TypeError("decrypt() requires a str")
    cipher = _load_cipher()
    return cipher.decrypt(ciphertext.encode("ascii")).decode("utf-8")


def generate_key() -> str:
    """Mint a fresh Fernet key. Caller is responsible for storing it
    safely — typically as ``NYMERIA_SECRETS_KEY`` in ``.env.docker``.
    """
    return Fernet.generate_key().decode("ascii")


__all__ = [
    "SecretsKeyMissing",
    "SecretsKeyInvalid",
    "SECRETS_KEY_ENV_VAR",
    "has_secrets_key",
    "encrypt",
    "decrypt",
    "generate_key",
    "InvalidToken",
]
