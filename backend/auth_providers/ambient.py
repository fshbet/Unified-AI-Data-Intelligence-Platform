"""Ambient identity: whatever the host already has.

Application Default Credentials on GCP, Managed Identity on Azure, integrated auth on Windows.
The best option when it is available, because there is no secret to store, rotate or leak.

When nothing is available this reports **what it tried**. "No ambient credentials found" with no
detail is a miserable thing to debug on someone else's server.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import httpx

from backend.auth_providers.base import AuthProvider, Credential, register_auth
from backend.connectors.base import ConfigField

GCE_METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
AZURE_IMDS = "http://169.254.169.254/metadata/identity/oauth2/token"


class AuthError(RuntimeError):
    pass


@register_auth
class AmbientAuth(AuthProvider):
    mode = "ambient"
    display_name = "Ambient / managed identity"
    description = "Use the identity this host already has — GCP ADC, Azure Managed Identity, or Windows integrated auth. No secret is stored."
    config_fields = [
        ConfigField("resource", "Resource / audience", required=False,
                    help="e.g. https://graph.microsoft.com or https://database.windows.net"),
        ConfigField("allow_integrated", "Allow OS integrated auth", type="boolean", default=True, required=False),
    ]

    def acquire(self, state: dict | None = None) -> Credential:
        tried: list[str] = []
        for attempt in (self._gcp_metadata, self._gcloud_cli, self._azure_imds, self._azure_cli, self._integrated):
            try:
                cred = attempt()
                if cred is not None:
                    return cred
                tried.append(attempt.__doc__ or attempt.__name__)
            except Exception as exc:  # noqa: BLE001 - each source is best-effort by design
                tried.append(f"{attempt.__doc__ or attempt.__name__} ({type(exc).__name__})")
        raise AuthError("No ambient credentials are available on this host. Tried: " + "; ".join(tried))

    # ------------------------------------------------------------------ sources
    def _gcp_metadata(self) -> Credential | None:
        """GCE/Cloud Run metadata server"""
        with httpx.Client(timeout=2) as c:
            r = c.get(GCE_METADATA, headers={"Metadata-Flavor": "Google"})
        if r.status_code != 200:
            return None
        body = r.json()
        return Credential(kind="bearer", secret=body["access_token"],
                          expires_at=_in(int(body.get("expires_in", 3600))),
                          metadata={"source": "gcp_metadata"})

    def _gcloud_cli(self) -> Credential | None:
        """gcloud application-default credentials"""
        if not (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or shutil.which("gcloud")):
            return None
        out = subprocess.run(["gcloud", "auth", "application-default", "print-access-token"],
                             capture_output=True, text=True, timeout=20)
        token = out.stdout.strip()
        if out.returncode != 0 or not token:
            return None
        return Credential(kind="bearer", secret=token, expires_at=_in(3600),
                          metadata={"source": "gcloud_adc"})

    def _azure_imds(self) -> Credential | None:
        """Azure Managed Identity (IMDS)"""
        resource = self.config.get("resource") or "https://graph.microsoft.com"
        with httpx.Client(timeout=2) as c:
            r = c.get(AZURE_IMDS, params={"api-version": "2018-02-01", "resource": resource},
                      headers={"Metadata": "true"})
        if r.status_code != 200:
            return None
        body = r.json()
        return Credential(kind="bearer", secret=body["access_token"],
                          expires_at=_in(int(body.get("expires_in", 3600))),
                          metadata={"source": "azure_imds", "resource": resource})

    def _azure_cli(self) -> Credential | None:
        """az CLI login"""
        if not shutil.which("az"):
            return None
        resource = self.config.get("resource") or "https://graph.microsoft.com"
        out = subprocess.run(["az", "account", "get-access-token", "--resource", resource, "-o", "tsv",
                              "--query", "accessToken"], capture_output=True, text=True, timeout=30)
        token = out.stdout.strip()
        if out.returncode != 0 or not token:
            return None
        return Credential(kind="bearer", secret=token, expires_at=_in(3600),
                          metadata={"source": "azure_cli", "resource": resource})

    def _integrated(self) -> Credential | None:
        """OS integrated auth (Windows / peer)"""
        if not self.config.get("allow_integrated", True):
            return None
        # Nothing to fetch: the driver negotiates with the OS. `kind="none"` tells the connector
        # to build a trusted-connection string rather than attach a header.
        return Credential(kind="none", secret="", metadata={"source": "integrated"})


def _in(seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)
