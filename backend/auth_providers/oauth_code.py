"""Browser sign-in: OAuth 2.0 authorization code with PKCE.

The user consents as themselves, so the connection sees exactly what they can see. This is the
right default for "connect my Google account" and for Microsoft delegated permissions.

Two details that are not optional:

* **PKCE**, even though we hold a client secret. The redirect lands on a loopback URL, and a
  code intercepted there is useless without the verifier.
* **`state`**, verified, single-use, and short-lived. A callback whose state we did not issue is
  rejected outright rather than "helpfully" exchanged.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from backend.auth_providers.base import AuthProvider, Credential, InteractiveStart, register_auth
from backend.connectors.base import ConfigField

PRESETS = {
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": "https://www.googleapis.com/auth/drive.readonly https://www.googleapis.com/auth/spreadsheets.readonly",
        # Without BOTH of these Google issues no refresh token on a repeat authorisation, and
        # the connection silently dies an hour later.
        "extra_auth_params": {"access_type": "offline", "prompt": "consent"},
    },
    "microsoft": {
        "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        # offline_access or there is no refresh token at all.
        "scopes": "offline_access User.Read Files.Read Sites.Read.All",
        "extra_auth_params": {},
    },
}


class AuthError(RuntimeError):
    pass


@register_auth
class OAuthCodeAuth(AuthProvider):
    mode = "oauth_code"
    display_name = "Sign in with a browser"
    description = "You authorise in a browser window and the connection acts as you, with your own permissions."
    interactive = True
    config_fields = [
        ConfigField("platform", "Platform", type="select", options=["google", "microsoft", "custom"], default="google"),
        ConfigField("client_id", "Client ID"),
        ConfigField("client_secret", "Client secret", type="password", required=False),
        ConfigField("scopes", "Scopes", required=False, help="Space-separated. Defaults to a read-only set."),
        ConfigField("tenant_id", "Directory (tenant) ID", required=False, help="Microsoft only; 'common' for any account"),
        ConfigField("authorize_url", "Authorization endpoint", required=False),
        ConfigField("token_url", "Token endpoint", required=False),
    ]

    def _preset(self) -> dict:
        return PRESETS.get(self.config.get("platform", "google"), {"extra_auth_params": {}})

    def _endpoint(self, which: str) -> str:
        if self.config.get(which):
            return self.config[which]
        preset = self._preset()
        url = preset.get(which)
        if not url:
            raise AuthError(f"No {which} configured for this platform")
        tenant = self.config.get("tenant_id")
        if tenant and self.config.get("platform") == "microsoft":
            url = url.replace("/common/", f"/{tenant}/")
        return url

    def _scopes(self) -> str:
        return self.config.get("scopes") or self._preset().get("scopes", "")

    # ------------------------------------------------------------------ interactive
    def begin(self, redirect_uri: str | None = None) -> InteractiveStart:
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(32)
        params = {
            "response_type": "code",
            "client_id": self.config["client_id"],
            "redirect_uri": redirect_uri,
            "scope": self._scopes(),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            **self._preset().get("extra_auth_params", {}),
        }
        start = InteractiveStart(mode="redirect", url=f"{self._endpoint('authorize_url')}?{urlencode(params)}")
        # The caller persists these and must hand them back to complete(); they never leave
        # this machine and the state is single-use.
        object.__setattr__(start, "_pkce", {"state": state, "code_verifier": verifier, "redirect_uri": redirect_uri})
        return start

    def complete(self, payload: dict, state: dict | None = None) -> Credential:
        state = state or {}
        if not state.get("state") or payload.get("state") != state["state"]:
            raise AuthError("OAuth state mismatch — this callback did not come from a flow this server started")
        if payload.get("error"):
            raise AuthError(f"Authorisation was refused: {payload.get('error_description') or payload['error']}")
        code = payload.get("code")
        if not code:
            raise AuthError("No authorization code in the callback")

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.config["client_id"],
            # Must be byte-identical to the one used at the authorize step or the exchange 400s.
            "redirect_uri": state.get("redirect_uri"),
            "code_verifier": state.get("code_verifier"),
        }
        if self.config.get("client_secret"):
            data["client_secret"] = self.config["client_secret"]
        return self._exchange(data)

    # ------------------------------------------------------------------ refresh
    def refresh(self, cred: Credential) -> Credential:
        if not cred.refresh_token:
            raise AuthError("This connection has no refresh token; the user must sign in again")
        data = {"grant_type": "refresh_token", "refresh_token": cred.refresh_token,
                "client_id": self.config["client_id"]}
        if self.config.get("client_secret"):
            data["client_secret"] = self.config["client_secret"]
        new = self._exchange(data)
        # Google omits the refresh token on a refresh response; keep the one we already have or
        # the connection can only ever be refreshed once.
        if not new.refresh_token:
            new.refresh_token = cred.refresh_token
        return new

    def _exchange(self, data: dict) -> Credential:
        with httpx.Client(timeout=30) as c:
            r = c.post(self._endpoint("token_url"), data=data)
        if r.status_code >= 400:
            raise AuthError(f"Token exchange failed ({r.status_code}): {r.text[:300]}")
        body = r.json()
        return Credential(
            kind="bearer", secret=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=int(body.get("expires_in", 3600))),
            scopes=tuple((body.get("scope") or self._scopes()).split()),
            metadata={"platform": self.config.get("platform")},
        )

    def acquire(self, state: dict | None = None) -> Credential:
        raise AuthError("Browser sign-in cannot be performed without a user; use begin()/complete()")
