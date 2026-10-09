"""Microsoft 365: Excel workbooks on OneDrive/SharePoint, and SharePoint lists.

REST against Microsoft Graph, so there is no MSAL dependency here — tokens come from the
`backend.auth_providers` framework, which covers application permissions (service principal),
delegated browser sign-in, and device code.

The thing that catches people out is application vs delegated permissions, so the error paths
below name it explicitly rather than passing Graph's wording through.
"""
from __future__ import annotations

import time
from typing import Any, Iterator

import httpx
import pandas as pd

from backend.connectors.base import ConfigField
from backend.connectors.google_connector import coerce_columns, normalise_headers, _safe_table_name
from backend.connectors.materialized import MaterializedConnector
from backend.connectors.registry import register

GRAPH = "https://graph.microsoft.com/v1.0"


class MicrosoftError(RuntimeError):
    pass


class _GraphBase(MaterializedConnector):
    category = "cloud"
    supported_auth = ("service_account", "oauth_code", "device_code", "credentials")

    def credential(self):
        cred = self.config.get("_credential")
        if cred is None:
            raise MicrosoftError("No credential attached to this connection — authorise it first")
        return cred

    def _base(self) -> str:
        return (self.config.get("api_base") or GRAPH).rstrip("/")

    def _get(self, url: str, **params) -> dict:
        """GET with the two behaviours Graph requires: honour Retry-After, and diagnose 403."""
        full = url if url.startswith("http") else f"{self._base()}{url}"
        for attempt in range(4):
            with httpx.Client(timeout=60) as c:
                r = c.get(full, headers={"Authorization": f"Bearer {self.credential().secret}"},
                          params=params or None)
            if r.status_code == 429:
                # Graph throttles hard and ignoring Retry-After gets the whole tenant blocked.
                wait = int(r.headers.get("Retry-After", "5"))
                time.sleep(min(wait, 60))
                continue
            if r.status_code == 403:
                raise MicrosoftError(
                    "Access denied (403). If this connection uses a service principal, the app "
                    "registration needs admin consent for its APPLICATION permissions (for "
                    "example Files.Read.All) — delegated permissions do not apply to it. "
                    f"Detail: {r.text[:200]}")
            if r.status_code >= 400:
                raise MicrosoftError(f"Microsoft Graph {r.status_code}: {r.text[:300]}")
            return r.json()
        raise MicrosoftError("Microsoft Graph kept throttling the request (429) after several retries")

    def _paged(self, url: str, **params) -> Iterator[dict]:
        body = self._get(url, **params)
        while True:
            yield from body.get("value", [])
            nxt = body.get("@odata.nextLink")
            if not nxt:
                return
            # Follow the link verbatim. Rebuilding it from parts breaks on the second page
            # because the $skiptoken is opaque.
            body = self._get(nxt)


@register
class ExcelOnlineConnector(_GraphBase):
    type_key = "ms_excel"
    display_name = "Excel (OneDrive / SharePoint)"
    config_fields = [
        ConfigField("drive_id", "Drive ID", required=False, help="Blank uses the site's default document library"),
        ConfigField("site_id", "Site ID", required=False, help="SharePoint site; blank means OneDrive"),
        ConfigField("item_path", "Workbook path", help="e.g. /Finance/Budget.xlsx"),
        ConfigField("worksheets", "Worksheets", required=False, help="Comma-separated. Blank means all."),
        ConfigField("header_row", "Header row", type="integer", default=1, required=False),
        ConfigField("api_base", "API base URL", required=False, help="Override for testing"),
    ]

    def _item(self) -> str:
        path = self.config["item_path"].lstrip("/")
        if self.config.get("drive_id"):
            return f"/drives/{self.config['drive_id']}/root:/{path}:"
        if self.config.get("site_id"):
            return f"/sites/{self.config['site_id']}/drive/root:/{path}:"
        return f"/me/drive/root:/{path}:"

    def test_connection(self) -> dict[str, Any]:
        try:
            sheets = [s["name"] for s in self._get(f"{self._item()}/workbook/worksheets").get("value", [])]
            return {"ok": True, "message": f"{len(sheets)} worksheet(s)", "tables": sheets}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)[:500]}

    def load_frames(self) -> Iterator[tuple[str, pd.DataFrame]]:
        wanted_raw = (self.config.get("worksheets") or "").strip()
        wanted = {w.strip() for w in wanted_raw.split(",") if w.strip()} if wanted_raw else None
        header_row = int(self.config.get("header_row") or 1)
        for sheet in self._get(f"{self._item()}/workbook/worksheets").get("value", []):
            name = sheet["name"]
            if wanted and name not in wanted:
                continue
            used = self._get(f"{self._item()}/workbook/worksheets/{name}/usedRange")
            values = used.get("values") or []
            if len(values) < header_row:
                continue
            # Same header handling as Sheets — blanks, duplicates and stray spaces are not a
            # Google problem, they are a spreadsheet problem.
            header = normalise_headers(values[header_row - 1])
            rows = [(r + [None] * len(header))[: len(header)] for r in values[header_row:]]
            yield _safe_table_name(name), coerce_columns(pd.DataFrame(rows, columns=header))


@register
class SharePointListConnector(_GraphBase):
    type_key = "sharepoint_list"
    display_name = "SharePoint List"
    config_fields = [
        ConfigField("site_id", "Site ID"),
        ConfigField("lists", "Lists to include", required=False, help="Comma-separated display names. Blank means all."),
        ConfigField("api_base", "API base URL", required=False, help="Override for testing"),
    ]

    def test_connection(self) -> dict[str, Any]:
        try:
            names = [lst["displayName"] for lst in self._paged(f"/sites/{self.config['site_id']}/lists")]
            return {"ok": True, "message": f"{len(names)} list(s)", "tables": names}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)[:500]}

    def load_frames(self) -> Iterator[tuple[str, pd.DataFrame]]:
        site = self.config["site_id"]
        raw = (self.config.get("lists") or "").strip()
        wanted = {w.strip() for w in raw.split(",") if w.strip()} if raw else None
        for lst in self._paged(f"/sites/{site}/lists"):
            if wanted and lst["displayName"] not in wanted:
                continue
            # Map internal field names (OData__x0020_Cost) back to display names. The raw ones
            # are unusable in a prompt or a UI.
            display = {c["name"]: c.get("displayName") or c["name"]
                       for c in self._get(f"/sites/{site}/lists/{lst['id']}/columns").get("value", [])}
            rows = []
            for item in self._paged(f"/sites/{site}/lists/{lst['id']}/items", expand="fields"):
                fields = item.get("fields", {})
                rows.append({display.get(k, k): v for k, v in fields.items() if not k.startswith("@")})
            if rows:
                yield _safe_table_name(lst["displayName"]), coerce_columns(pd.DataFrame(rows))
