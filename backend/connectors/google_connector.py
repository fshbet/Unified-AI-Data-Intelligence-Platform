"""Google Sheets and BigQuery.

REST only — no Google SDK, so there is no optional dependency to install and nothing to go stale.
Authentication comes from the `backend.auth_providers` framework, so the same connector works
with a service account, a browser sign-in, or the host's ambient identity, and the connector
itself knows nothing about any of them.
"""
from __future__ import annotations

import re
from typing import Any, Iterator

import httpx
import pandas as pd

from backend.connectors.base import ConfigField, TableInfo
from backend.connectors.materialized import MaterializedConnector
from backend.connectors.registry import register

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
BQ_API = "https://bigquery.googleapis.com/bigquery/v2"


class GoogleError(RuntimeError):
    pass


def _headers(connector: "_GoogleBase") -> dict[str, str]:
    cred = connector.credential()
    if cred.kind != "bearer":
        raise GoogleError(f"Google needs a bearer token; this connection supplied '{cred.kind}'")
    return {"Authorization": f"Bearer {cred.secret}"}


def normalise_headers(raw: list[Any]) -> list[str]:
    """Real spreadsheets have blank cells, duplicate names and stray whitespace in the header row.

    Dropping blanks would silently shift every column to the left, so they are named positionally
    instead. Duplicates get a numeric suffix rather than overwriting each other.
    """
    out: list[str] = []
    seen: dict[str, int] = {}
    for i, cell in enumerate(raw):
        name = re.sub(r"\s+", " ", str(cell or "").strip())
        if not name:
            name = f"column_{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out


def coerce_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Type a column only if EVERY value parses. One stray 'n/a' makes the column text —
    guessing wrong here produces silently dropped rows later."""
    for col in df.columns:
        series = df[col]
        non_empty = series[series.astype(str).str.strip() != ""]
        if non_empty.empty:
            continue
        numeric = pd.to_numeric(non_empty, errors="coerce")
        if numeric.notna().all():
            df[col] = pd.to_numeric(series, errors="coerce")
            continue
        dates = pd.to_datetime(non_empty, errors="coerce", format="mixed")
        if dates.notna().all():
            df[col] = pd.to_datetime(series, errors="coerce", format="mixed")
    return df


class _GoogleBase(MaterializedConnector):
    category = "cloud"

    def credential(self):
        """Resolved by the API layer before use; see backend.auth_providers.service."""
        cred = self.config.get("_credential")
        if cred is None:
            raise GoogleError("No credential attached to this connection — authorise it first")
        return cred

    def _get(self, url: str, **params) -> dict:
        with httpx.Client(timeout=60) as c:
            r = c.get(url, headers=_headers(self), params=params or None)
        if r.status_code == 404:
            meta = getattr(self.credential(), "metadata", {}) or {}
            who = meta.get("client_email")
            hint = (f" The connection authenticates as {who}; a service account is a separate "
                    f"identity, so the file or dataset must be shared with that address."
                    if who else "")
            raise GoogleError(f"Not found (404).{hint}")
        if r.status_code >= 400:
            raise GoogleError(f"Google API {r.status_code}: {r.text[:300]}")
        return r.json()


@register
class GoogleSheetsConnector(_GoogleBase):
    type_key = "google_sheets"
    display_name = "Google Sheets"
    supported_auth = ("service_account", "oauth_code", "ambient")
    config_fields = [
        ConfigField("spreadsheet_id", "Spreadsheet ID", help="The long id in the URL: /spreadsheets/d/<THIS>/edit"),
        ConfigField("sheets", "Sheets to include", required=False, help="Comma-separated. Blank means every tab."),
        ConfigField("header_row", "Header row", type="integer", default=1, required=False),
        ConfigField("api_base", "API base URL", required=False, help="Override for testing"),
    ]

    def _base(self) -> str:
        return (self.config.get("api_base") or SHEETS_API).rstrip("/")

    def _meta(self) -> dict:
        return self._get(f"{self._base()}/{self.config['spreadsheet_id']}", includeGridData="false")

    def test_connection(self) -> dict[str, Any]:
        try:
            meta = self._meta()
            tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]
            return {"ok": True, "message": f"{meta.get('properties', {}).get('title', 'Spreadsheet')} · {len(tabs)} tab(s)",
                    "tables": tabs}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)[:500]}

    def _wanted(self) -> set[str] | None:
        raw = (self.config.get("sheets") or "").strip()
        return {s.strip() for s in raw.split(",") if s.strip()} or None if raw else None

    def load_frames(self) -> Iterator[tuple[str, pd.DataFrame]]:
        meta = self._meta()
        wanted = self._wanted()
        header_row = int(self.config.get("header_row") or 1)
        for sheet in meta.get("sheets", []):
            title = sheet["properties"]["title"]
            if wanted and title not in wanted:
                continue
            # UNFORMATTED_VALUE or every number arrives as a display string like "1,234.50".
            body = self._get(
                f"{self._base()}/{self.config['spreadsheet_id']}/values/{httpx.URL(path=title).path.lstrip('/')}",
                valueRenderOption="UNFORMATTED_VALUE", dateTimeRenderOption="FORMATTED_STRING")
            values = body.get("values") or []
            if len(values) < header_row:
                continue
            header = normalise_headers(values[header_row - 1])
            rows = [r + [None] * (len(header) - len(r)) for r in values[header_row:]]
            rows = [r[: len(header)] for r in rows]
            df = coerce_columns(pd.DataFrame(rows, columns=header))
            yield _safe_table_name(title), df


@register
class BigQueryConnector(_GoogleBase):
    type_key = "bigquery"
    display_name = "Google BigQuery"
    supported_auth = ("service_account", "oauth_code", "ambient")
    config_fields = [
        ConfigField("project_id", "Project ID"),
        ConfigField("dataset_id", "Dataset ID"),
        ConfigField("location", "Location", required=False, default="US"),
        ConfigField("max_bytes_billed", "Max bytes billed", type="integer", default=1_000_000_000, required=False,
                    help="A hard ceiling on every query. Protects against an accidental full-table scan."),
        ConfigField("tables", "Tables to include", required=False, help="Comma-separated. Blank means all."),
        ConfigField("row_limit", "Rows per table", type="integer", default=100_000, required=False),
        ConfigField("api_base", "API base URL", required=False, help="Override for testing"),
    ]

    def _base(self) -> str:
        return (self.config.get("api_base") or BQ_API).rstrip("/")

    def _query(self, sql: str) -> dict:
        from backend.query_engine.sql_safety import validate_read_only

        # BigQuery is SQL like any other source, so it goes through the same read-only gate.
        validate_read_only(sql)
        body = {
            "query": sql,
            "useLegacySql": False,
            "location": self.config.get("location") or "US",
            # Never optional: a connector that can run an unbounded scan against a billed API
            # will eventually produce a memorable invoice.
            "maximumBytesBilled": str(int(self.config.get("max_bytes_billed") or 1_000_000_000)),
        }
        with httpx.Client(timeout=120) as c:
            r = c.post(f"{self._base()}/projects/{self.config['project_id']}/queries",
                       headers=_headers(self), json=body)
        if r.status_code >= 400:
            raise GoogleError(f"BigQuery {r.status_code}: {r.text[:400]}")
        return r.json()

    def test_connection(self) -> dict[str, Any]:
        try:
            tables = self._list_tables()
            return {"ok": True, "message": f"{len(tables)} table(s) in {self.config['dataset_id']}",
                    "tables": tables}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)[:500]}

    def _list_tables(self) -> list[str]:
        ds = self.config["dataset_id"]
        out = self._query(f"SELECT table_name FROM `{self.config['project_id']}.{ds}.INFORMATION_SCHEMA.TABLES`")
        names = [row["f"][0]["v"] for row in out.get("rows", [])]
        wanted = (self.config.get("tables") or "").strip()
        if wanted:
            keep = {t.strip() for t in wanted.split(",") if t.strip()}
            names = [n for n in names if n in keep]
        return names

    def load_frames(self) -> Iterator[tuple[str, pd.DataFrame]]:
        limit = int(self.config.get("row_limit") or 100_000)
        for name in self._list_tables():
            out = self._query(
                f"SELECT * FROM `{self.config['project_id']}.{self.config['dataset_id']}.{name}` LIMIT {limit}")
            cols = [f["name"] for f in out.get("schema", {}).get("fields", [])]
            rows = [[cell.get("v") for cell in row["f"]] for row in out.get("rows", [])]
            yield _safe_table_name(name), coerce_columns(pd.DataFrame(rows, columns=cols))


def _safe_table_name(raw: str) -> str:
    """Tab names like 'Q3 Results (final)' must survive into DuckDB as a usable identifier."""
    name = re.sub(r"[^0-9a-zA-Z_]+", "_", raw.strip()).strip("_").lower()
    return name or "sheet"
