"""Device code flow — for a server with no browser.

The user is shown a short code, types it on their phone, and the server polls until they finish.
The polling contract is where implementations usually go wrong: `authorization_pending` is not an
error, and `slow_down` means increase the interval rather than give up.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from backend.auth_providers.base import AuthProvider, Credential, InteractiveStart, register_auth
from backend.connectors.base import ConfigField

PRESETS = {
    "google": {
        "device_url": "https://oauth2.googleapis.com/device/code",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": "https://www.googleapis.com/auth/drive.readonly",
    },
    "microsoft": {
        "device_url": "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scopes": "offline_access User.Read Files.Read",
    },
}

PENDING = {"authorization_pending", "slow_down"}


class AuthError(RuntimeError):
    pass


class AuthPending(Exception):
    """Not yet authorised. Carries the interval the server wants us to use next."""

    def __init__(self, interval: int) -> None:
        super().__init__("authorization pending")
        self.interval = interval


@register_auth
class DeviceCodeAuth(AuthProvider):
    mode = "device_code"
    display_name = "Device code"
    description = "For a server with no browser: a short code is shown, and you authorise it on another device."
    interactive = True
    config_fields = [
        ConfigField("platform", "Platform", type="select", options=["google", "microsoft"], default="microsoft"),
        ConfigField("client_id", "Client ID"),
        ConfigField("client_secret", "Client secret", type="password", required=False),
        ConfigField("scopes", "Scopes", required=False),
        ConfigField("tenant_id", "Directory (tenant) ID", required=False),
        ConfigField("device_url", "Device authorization endpoint", required=False),
        ConfigField("token_url", "Token endpoint", required=False),
    ]

    def _preset(self) -> dict:
        return PRESETS.get(self.config.get("platform", "microsoft"), {})

    def _endpoint(self, which: str) -> str:
        url = self.config.get(which) or self._preset().get(which)
        if not url:
            raise AuthError(f"No {which} configured")
        tenant = self.config.get("tenant_id")
        if tenant and self.config.get("platform") == "microsoft":
            url = url.replace("/common/", f"/{tenant}/")
        return url

    def begin(self, redirect_uri: str | None = None) -> InteractiveStart:
        scopes = self.config.get("scopes") or self._preset().get("scopes", "")
        with httpx.Client(timeout=30) as c:
            r = c.post(self._endpoint("device_url"),
                       data={"client_id": self.config["client_id"], "scope": scopes})
        if r.status_code >= 400:
            raise AuthError(f"Could not start device flow ({r.status_code}): {r.text[:300]}")
        body = r.json()
        start = InteractiveStart(
            mode="device",
            url=body.get("verification_uri_complete") or body.get("verification_uri") or body.get("verification_url", ""),
            user_code=body.get("user_code"),
            expires_in=int(body.get("expires_in", 900)),
            poll_interval=int(body.get("interval", 5)),
            message=body.get("message"),
        )
        object.__setattr__(start, "_pkce", {"device_code": body["device_code"],
                                            "interval": int(body.get("interval", 5))})
        return start

    def complete(self, payload: dict, state: dict | None = None) -> Credential:
        """One poll. Raises AuthPending while the user has not finished."""
        state = state or {}
        device_code = state.get("device_code") or payload.get("device_code")
        if not device_code:
            raise AuthError("No device code in the pending flow")
        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "client_id": self.config["client_id"],
        }
        if self.config.get("client_secret"):
            data["client_secret"] = self.config["client_secret"]
        with httpx.Client(timeout=30) as c:
            r = c.post(self._endpoint("token_url"), data=data)
        body = r.json() if r.content else {}
        if r.status_code >= 400:
            error = body.get("error", "")
            if error in PENDING:
                interval = int(state.get("interval", 5))
                # slow_down is an instruction, not a failure: keep polling, less often.
                raise AuthPending(interval + 5 if error == "slow_down" else interval)
            if error == "expired_token":
                raise AuthError("The code expired before it was authorised. Start again.")
            raise AuthError(f"Device authorisation failed: {body.get('error_description') or error or r.text[:200]}")
        return Credential(
            kind="bearer", secret=body["access_token"], refresh_token=body.get("refresh_token"),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=int(body.get("expires_in", 3600))),
            scopes=tuple((body.get("scope") or "").split()),
            metadata={"platform": self.config.get("platform")},
        )

    def refresh(self, cred: Credential) -> Credential:
        if not cred.refresh_token:
            raise AuthError("No refresh token; the device must be authorised again")
        data = {"grant_type": "refresh_token", "refresh_token": cred.refresh_token,
                "client_id": self.config["client_id"]}
        if self.config.get("client_secret"):
            data["client_secret"] = self.config["client_secret"]
        with httpx.Client(timeout=30) as c:
            r = c.post(self._endpoint("token_url"), data=data)
        if r.status_code >= 400:
            raise AuthError(f"Refresh failed ({r.status_code}): {r.text[:300]}")
        body = r.json()
        return Credential(
            kind="bearer", secret=body["access_token"],
            refresh_token=body.get("refresh_token") or cred.refresh_token,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=int(body.get("expires_in", 3600))),
            scopes=cred.scopes, metadata=cred.metadata,
        )

    def acquire(self, state: dict | None = None) -> Credential:
        raise AuthError("Device code requires a user; use begin()/complete()")
