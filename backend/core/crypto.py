"""Symmetric encryption for secrets at rest (connector passwords, OAuth secrets, AI API keys)."""
from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from backend.core.config import settings

log = logging.getLogger(__name__)

# Every Fernet ciphertext starts with the version byte 0x80, which base64-encodes to this.
# It is what lets us tell "stored in plaintext" apart from "encrypted with a different key" —
# a distinction that matters enormously and that the previous implementation threw away.
FERNET_PREFIX = "gAAAAA"


class DecryptionError(RuntimeError):
    """A value is Fernet ciphertext but this key cannot open it — usually a rotated secret_key."""


def _fernet() -> Fernet:
    if settings.encryption_key:
        return Fernet(settings.encryption_key.encode())
    derived = hashlib.sha256(settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def looks_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(FERNET_PREFIX)


def decrypt(token: str) -> str:
    """Decrypt a stored secret.

    Returns a legacy plaintext value unchanged (so an existing install keeps working), but
    **raises** when the value is real ciphertext this key cannot open. The previous version
    returned the input on any failure, which silently turned an unreadable credential into a
    password attempt — and hid the fact that several fields were never encrypted at all.
    """
    if not looks_encrypted(token):
        return token  # legacy plaintext; `scripts/encrypt_existing_secrets.py` migrates these
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise DecryptionError(
            "A stored secret could not be decrypted. EDI_SECRET_KEY (or EDI_ENCRYPTION_KEY) has "
            "most likely changed since it was saved; restore the original key or re-enter the "
            "credential."
        ) from exc


# Substring markers rather than an exact-match set. The old exact-match set silently left
# `client_secret`, `pat_secret`, `dsn`, `access_token` and `credentials_json` in PLAINTEXT,
# because none of those strings was literally a member. Matching on substrings also means a
# field added later is protected by default instead of by remembering to edit this list.
SECRET_MARKERS = (
    "password", "passwd", "secret", "token", "api_key", "apikey", "apisecret",
    "credential", "private_key", "privatekey", "dsn", "passphrase", "auth",
)
# Fields whose name matches a marker but which are NOT secret and must stay readable.
SECRET_EXEMPT = {"auth_mode", "auth_type", "authority", "token_url", "auth_url", "authorize_url",
                 "pat_name", "api_key_header", "header_name", "credentials_path"}


def is_secret_field(name: str) -> bool:
    n = name.lower()
    if n in SECRET_EXEMPT:
        return False
    return any(m in n for m in SECRET_MARKERS)


def encrypt_config(cfg: dict) -> dict:
    return {k: (encrypt(v) if is_secret_field(k) and isinstance(v, str) and v and not looks_encrypted(v) else v)
            for k, v in cfg.items()}


def decrypt_config(cfg: dict) -> dict:
    return {k: (decrypt(v) if is_secret_field(k) and isinstance(v, str) and v else v)
            for k, v in cfg.items()}


def mask_config(cfg: dict) -> dict:
    return {k: ("********" if is_secret_field(k) and v else v) for k, v in cfg.items()}


def demo() -> None:
    """Self-check: the exact-match bug must not come back."""
    for field in ("client_secret", "pat_secret", "dsn", "access_token", "credentials_json",
                  "password", "api_key", "refresh_token", "private_key"):
        assert is_secret_field(field), f"{field} would be stored in plaintext"
    for field in ("auth_mode", "authority", "token_url", "pat_name", "header_name",
                  "spreadsheet_id", "host", "port", "database"):
        assert not is_secret_field(field), f"{field} would be needlessly encrypted"

    cfg = {"host": "db.internal", "password": "hunter2", "client_secret": "sp-secret", "dsn": "postgres://u:p@h/d"}
    enc = encrypt_config(cfg)
    assert enc["host"] == "db.internal"
    assert all(looks_encrypted(enc[k]) for k in ("password", "client_secret", "dsn"))
    assert decrypt_config(enc) == cfg
    assert mask_config(cfg)["client_secret"] == "********"
    # Encrypting twice must not double-wrap.
    assert encrypt_config(enc) == enc
    # Legacy plaintext still reads; real ciphertext under a foreign key raises.
    assert decrypt("plain-legacy-value") == "plain-legacy-value"
    try:
        decrypt(FERNET_PREFIX + "Zm9ydGhld2luQEV4YW1wbGU=")
        raise AssertionError("a corrupt ciphertext must raise, not be returned as a password")
    except DecryptionError:
        pass
    print("crypto self-check passed")


if __name__ == "__main__":
    demo()
