"""Authentication modes, as plugins.

A connector declares which modes it supports and implements none of them:

    supported_auth = ("service_account", "oauth_code", "ambient")

That matters because the same system is reached different ways by different customers. One
organisation wires Google up with a service account for unattended sync; the next wants each
analyst to sign in as themselves; a third runs on GCP and wants no credentials at all. Without
this split every connector grows three code paths and they all drift.

    service_account  Google SA key, Entra app registration (secret or certificate)
    oauth_code       browser redirect + PKCE — the user consents as themselves
    device_code      headless box: the user types a code on their phone
    credentials      username/password, DSN, API key — databases and plain REST
    ambient          GCP ADC, Azure Managed Identity, Windows integrated auth
"""
from backend.auth_providers.base import (
    AuthProvider,
    Credential,
    InteractiveStart,
    create_auth,
    get_auth_class,
    list_auth_modes,
    register_auth,
    supported_modes_for,
)

__all__ = [
    "AuthProvider", "Credential", "InteractiveStart", "create_auth", "get_auth_class",
    "list_auth_modes", "register_auth", "supported_modes_for",
]
