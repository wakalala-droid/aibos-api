"""
AI-BOS — application-level field encryption.

One narrow job: seal a single sensitive string (today: guests.id_document_number,
migration 0015) with Fernet BEFORE it ever reaches Supabase, and open it again only
for an authorised read. Supabase's transparent at-rest encryption does not protect
against a leaked service-role key — and the entire API runs on that key — so the
plaintext must never touch a row.

Key source: env `FIELD_ENCRYPTION_KEY`, a urlsafe-base64 32-byte Fernet key
(generate once with `python -c "from cryptography.fernet import Fernet;
print(Fernet.generate_key().decode())"` and set it on Railway).

Fail-closed contract: if no key is configured we REFUSE to store the value rather
than silently writing plaintext. A caller with no ID number to store is unaffected
— encryption only engages when there is something to protect.
"""

import os
import logging

log = logging.getLogger("aibos.crypto")

_ENV_KEY = "FIELD_ENCRYPTION_KEY"
# Ciphertext prefix so a read can tell a sealed value from a legacy/plaintext one
# and never hand back a raw token as if it were the number.
_PREFIX = "enc:v1:"


class FieldCryptoUnavailable(RuntimeError):
    """Raised when encryption is requested but no key is configured — fail closed."""


def _fernet():
    key = os.environ.get(_ENV_KEY)
    if not key:
        raise FieldCryptoUnavailable(
            f"{_ENV_KEY} is not set — cannot store sensitive fields. Generate a Fernet "
            "key and set it on the server before capturing guest ID documents."
        )
    from cryptography.fernet import Fernet  # imported lazily so the dep is only

    try:                                    # needed when the feature is actually used
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as exc:  # noqa: BLE001 — a malformed key is an operator error
        raise FieldCryptoUnavailable(f"{_ENV_KEY} is not a valid Fernet key: {exc}")


def is_configured() -> bool:
    """True when a usable key is present — lets callers degrade instead of 500."""
    try:
        _fernet()
        return True
    except FieldCryptoUnavailable:
        return False


def encrypt(plaintext: str | None) -> str | None:
    """Seal a value for storage. None/empty passes through untouched (nothing to hide)."""
    if plaintext is None or str(plaintext).strip() == "":
        return None
    token = _fernet().encrypt(str(plaintext).encode()).decode()
    return _PREFIX + token


def decrypt(stored: str | None) -> str | None:
    """
    Open a sealed value. A value without our prefix is returned as-is (tolerates a
    pre-encryption/legacy row rather than throwing). A prefixed value that won't
    decrypt raises — that is real corruption or a rotated key, not something to hide.
    """
    if stored is None or stored == "":
        return None
    if not stored.startswith(_PREFIX):
        return stored
    token = stored[len(_PREFIX):]
    return _fernet().decrypt(token.encode()).decode()


def mask(stored: str | None) -> str | None:
    """
    Safe-for-display form: the last 4 characters of the real number, rest redacted,
    without decrypting into any log or list response. Returns None when absent.
    """
    if not stored:
        return None
    try:
        plain = decrypt(stored)
    except Exception:  # noqa: BLE001 — never let masking leak a stack trace / raw token
        return "•••• (sealed)"
    if not plain:
        return None
    tail = plain[-4:] if len(plain) >= 4 else plain
    return "•••• " + tail
