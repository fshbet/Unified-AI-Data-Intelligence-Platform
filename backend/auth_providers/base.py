"""The AuthProvider contract and its registry.

Mirrors `backend.connectors.registry` deliberately — same `@register` decorator shape, same
`_LOADED` autoload guard, same `config_fields`-drives-the-UI rule — so there is one plugin
idiom in the codebase rather than two.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar

from backend.connectors.base import ConfigField

log = logging.getLogger(__name__)


@dataclass
class Credential:
    """What a connector actually needs in order to talk to a system."""

    kind: str  # bearer | basic | api_key | dsn | sa_key | none
    secret: str = ""
    expires_at: datetime | None = None
    refresh_token: str | None = None
    username: str | None = None
    scopes: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, skew_seconds: int = 120) -> bool:
        if self.expires_at is None:
            return False
        exp = self.expires_at if self.expires_at.tzinfo else self.expires_at.replace(tzinfo=timezone.utc)
        return exp <= datetime.now(timezone.utc) + timedelta(seconds=skew_seconds)

    def header(self) -> dict[str, str]:
        if self.kind == "bearer":
            return {"Authorization": f"Bearer {self.secret}"}
        if self.kind == "api_key":
            return {self.metadata.get("header_name", "X-API-Key"): self.secret}
        if self.kind == "basic":
            import base64

            raw = base64.b64encode(f"{self.username or ''}:{self.secret}".encode()).decode()
            return {"Authorization": f"Basic {raw}"}
        return {}

    def __repr__(self) -> str:
        # Without this the first unhandled exception prints a live access token into the log.
        return f"Credential(kind={self.kind!r}, expires_at={self.expires_at!r}, scopes={len(self.scopes)})"


@dataclass(frozen=True)
class InteractiveStart:
    """What the UI needs to begin a flow that requires a human."""

    mode: str  # "redirect" | "device"
    url: str
    user_code: str | None = None
    expires_in: int | None = None
    poll_interval: int = 5
    message: str | None = None


class AuthProvider(ABC):
    mode: ClassVar[str]
    display_name: ClassVar[str]
    description: ClassVar[str] = ""
    config_fields: ClassVar[list[ConfigField]] = []
    interactive: ClassVar[bool] = False

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    @abstractmethod
    def acquire(self, state: dict[str, Any] | None = None) -> Credential:
        """Return a usable credential, performing whatever exchange the mode requires."""

    def refresh(self, cred: Credential) -> Credential:
        """Default: mint a new one. Correct for service accounts, which have no refresh token."""
        return self.acquire()

    def revoke(self, cred: Credential) -> None:
        return None

    # -- interactive modes only -------------------------------------------------
    def begin(self, redirect_uri: str | None = None) -> InteractiveStart:
        raise NotImplementedError(f"{self.mode} is not an interactive mode")

    def complete(self, payload: dict[str, Any], state: dict[str, Any] | None = None) -> Credential:
        raise NotImplementedError(f"{self.mode} is not an interactive mode")

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {
            "mode": cls.mode,
            "display_name": cls.display_name,
            "description": cls.description,
            "interactive": cls.interactive,
            "fields": [f.__dict__ for f in cls.config_fields],
        }


# ---------------------------------------------------------------------------- registry
_REGISTRY: dict[str, type[AuthProvider]] = {}
_LOADED = False


def register_auth(cls: type[AuthProvider]) -> type[AuthProvider]:
    _REGISTRY[cls.mode] = cls
    return cls


def _autoload() -> None:
    # Guard on an explicit flag, never on `_REGISTRY` being non-empty: any module that imports a
    # single provider directly would otherwise make the registry look populated and silently
    # suppress every provider that had not been imported yet. The connector registry shipped
    # with exactly that bug.
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    import backend.auth_providers as pkg

    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name in {"base", "service"}:
            continue
        try:
            importlib.import_module(f"{pkg.__name__}.{mod.name}")
        except ImportError as exc:
            # A mode whose optional SDK is missing must not take the application down.
            log.info("auth mode %s unavailable: %s", mod.name, exc)


def get_auth_class(mode: str) -> type[AuthProvider]:
    _autoload()
    if mode not in _REGISTRY:
        raise KeyError(f"unknown auth mode '{mode}'; available: {sorted(_REGISTRY)}")
    return _REGISTRY[mode]


def create_auth(mode: str, config: dict[str, Any]) -> AuthProvider:
    return get_auth_class(mode)(config)


def list_auth_modes() -> list[dict[str, Any]]:
    _autoload()
    return [cls.describe() for cls in sorted(_REGISTRY.values(), key=lambda c: c.mode)]


def supported_modes_for(connector_cls: type) -> list[dict[str, Any]]:
    """The modes a given connector accepts, in the order it declares them."""
    _autoload()
    declared = getattr(connector_cls, "supported_auth", ("credentials",))
    return [_REGISTRY[m].describe() for m in declared if m in _REGISTRY]
