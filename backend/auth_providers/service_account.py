"""Service accounts: a Google SA JSON key, or a Microsoft Entra app registration.

Both are the "unattended sync" option — the application acts as itself rather than as a person.
Neither issues a refresh token, so `refresh` just mints a new assertion, which the base class
already does.
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timedelta, timezone

import httpx

from backend.auth_providers.base import AuthProvider, Credential, register_auth
from backend.connectors.base import ConfigField

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


class AuthError(RuntimeError):
    pass


@register_auth
class ServiceAccountAuth(AuthProvider):
    mode = "service_account"
    display_name = "Service account / app registration"
    description = (
        "Unattended access. Google: paste the service-account JSON key and share the file or "
        "dataset with its client_email. Microsoft: tenant + client id + secret, with admin "
        "consent granted for the application permissions."
    )
    config_fields = [
        ConfigField("platform", "Platform", type="select", options=["google", "microsoft"], default="google"),
        ConfigField("credentials_json", "Service account JSON", type="textarea", required=False, help="Google only"),
        ConfigField("tenant_id", "Directory (tenant) ID", required=False, help="Microsoft only"),
        ConfigField("client_id", "Application (client) ID", required=False, help="Microsoft only"),
        ConfigField("client_secret", "Client secret", type="password", required=False, help="Microsoft only"),
        ConfigField("scopes", "Scopes", required=False, help="Space-separated. Defaults to a read-only set."),
        ConfigField("subject", "Impersonate user", required=False, help="Google domain-wide delegation only"),
        ConfigField("token_url", "Token endpoint", required=False, help="Override for testing"),
        ConfigField("authority", "Login authority", required=False, help="Override for testing"),
    ]

    # ------------------------------------------------------------------ google
    def _google(self) -> Credential:
        raw = self.config.get("credentials_json")
        if not raw:
            raise AuthError("No service account JSON supplied")
        try:
            key = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError as exc:
            raise AuthError(f"Service account JSON is not valid JSON: {exc}") from exc
        # Validate at save time rather than letting a sync fail three hours later with a
        # cryptography stack trace.
        for required in ("client_email", "private_key", "token_uri"):
            if not key.get(required):
                raise AuthError(f"Service account JSON is missing '{required}'")

        scopes = self.config.get("scopes") or "https://www.googleapis.com/auth/drive.readonly"
        now = int(time.time())
        claims = {
            "iss": key["client_email"], "scope": scopes,
            "aud": key.get("token_uri", GOOGLE_TOKEN_URL),
            "iat": now, "exp": now + 3600,
        }
        if self.config.get("subject"):
            claims["sub"] = self.config["subject"]

        assertion = _sign_rs256(claims, key["private_key"])
        url = self.config.get("token_url") or key.get("token_uri") or GOOGLE_TOKEN_URL
        with httpx.Client(timeout=30) as c:
            r = c.post(url, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                  "assertion": assertion})
        if r.status_code >= 400:
            raise AuthError(f"Google token exchange failed ({r.status_code}): {r.text[:300]}")
        data = r.json()
        return Credential(
            kind="bearer", secret=data["access_token"],
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in", 3600))),
            scopes=tuple(scopes.split()),
            metadata={"client_email": key["client_email"], "platform": "google"},
        )

    # ------------------------------------------------------------------ microsoft
    def _microsoft(self) -> Credential:
        cfg = self.config
        for required in ("tenant_id", "client_id", "client_secret"):
            if not cfg.get(required):
                raise AuthError(f"Microsoft service principal requires '{required}'")
        authority = (cfg.get("authority") or "https://login.microsoftonline.com").rstrip("/")
        url = cfg.get("token_url") or f"{authority}/{cfg['tenant_id']}/oauth2/v2.0/token"
        scope = cfg.get("scopes") or "https://graph.microsoft.com/.default"
        with httpx.Client(timeout=30) as c:
            r = c.post(url, data={"grant_type": "client_credentials", "client_id": cfg["client_id"],
                                  "client_secret": cfg["client_secret"], "scope": scope})
        if r.status_code >= 400:
            raise AuthError(_describe_aad(r))
        data = r.json()
        return Credential(
            kind="bearer", secret=data["access_token"],
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=int(data.get("expires_in", 3600))),
            scopes=tuple(scope.split()), metadata={"platform": "microsoft", "client_id": cfg["client_id"]},
        )

    def acquire(self, state: dict | None = None) -> Credential:
        return self._microsoft() if self.config.get("platform") == "microsoft" else self._google()


def _describe_aad(r: httpx.Response) -> str:
    """Entra's `error` field is useless on its own; the AADSTS code lives in the description."""
    try:
        body = r.json()
    except ValueError:
        return f"Microsoft token exchange failed ({r.status_code}): {r.text[:300]}"
    desc = body.get("error_description", "") or body.get("error", "")
    if "AADSTS65001" in desc or "AADSTS900" in desc:
        return ("The app registration has not been granted admin consent for its application "
                "permissions. An administrator must grant it in Entra ID > App registrations > "
                f"API permissions. ({desc[:200]})")
    if "AADSTS7000215" in desc:
        return f"Invalid client secret — it may have expired. ({desc[:200]})"
    if "AADSTS50020" in desc:
        return ("The account does not exist in this tenant — check the directory (tenant) ID, or "
                f"use 'common' for a multi-tenant app. ({desc[:200]})")
    return f"Microsoft token exchange failed ({r.status_code}): {desc[:300]}"


def _sign_rs256(claims: dict, private_key_pem: str) -> str:
    """RS256 JWT assertion. `cryptography` is already a dependency for the secret vault."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    def seg(obj: dict) -> bytes:
        return base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).rstrip(b"=")

    signing_input = seg({"alg": "RS256", "typ": "JWT"}) + b"." + seg(claims)
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return (signing_input + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()
