"""Symmetric encryption for secrets at rest (connector passwords, AI API keys)."""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from backend.core.config import settings


def _fernet() -> Fernet:
    if settings.encryption_key:
        return Fernet(settings.encryption_key.encode())
    derived = hashlib.sha256(settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return token  # plaintext (e.g. seeded fixtures); tolerate rather than crash


SECRET_FIELDS = {"password", "api_key", "token", "secret", "auth_token", "bearer_token"}


def encrypt_config(cfg: dict) -> dict:
    return {k: (encrypt(v) if k in SECRET_FIELDS and isinstance(v, str) and v else v) for k, v in cfg.items()}


def decrypt_config(cfg: dict) -> dict:
    return {k: (decrypt(v) if k in SECRET_FIELDS and isinstance(v, str) and v else v) for k, v in cfg.items()}


def mask_config(cfg: dict) -> dict:
    return {k: ("********" if k in SECRET_FIELDS and v else v) for k, v in cfg.items()}
