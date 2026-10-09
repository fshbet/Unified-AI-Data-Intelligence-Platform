"""Stored credentials: username/password, a DSN, or an API key.

No network call — `acquire` just wraps what was stored. Whether it actually works is proven by
the connector's own `test_connection`, which is the only thing that can really tell.
"""
from __future__ import annotations

from backend.auth_providers.base import AuthProvider, Credential, register_auth
from backend.connectors.base import ConfigField


@register_auth
class CredentialsAuth(AuthProvider):
    mode = "credentials"
    display_name = "Username & password / API key"
    description = "Credentials stored encrypted in this application. The common choice for databases and plain REST APIs."
    config_fields = [
        ConfigField("username", "Username", required=False),
        ConfigField("password", "Password", type="password", required=False),
        ConfigField("api_key", "API key", type="password", required=False, help="Use instead of username/password for token APIs"),
        ConfigField("header_name", "API key header", required=False, default="X-API-Key"),
        ConfigField("dsn", "Connection string", type="password", required=False, help="Overrides the fields above if set"),
    ]

    def acquire(self, state: dict | None = None) -> Credential:
        cfg = self.config
        if cfg.get("dsn"):
            return Credential(kind="dsn", secret=cfg["dsn"])
        if cfg.get("api_key"):
            return Credential(kind="api_key", secret=cfg["api_key"],
                              metadata={"header_name": cfg.get("header_name") or "X-API-Key"})
        return Credential(kind="basic", secret=cfg.get("password", ""), username=cfg.get("username"))
