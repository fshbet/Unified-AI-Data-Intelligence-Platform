"""Power BI semantic model connector.

Talks to the Power BI REST API, which needs no .NET/ADOMD and therefore runs anywhere:

  * metadata  — `EVALUATE INFO.VIEW.TABLES() / COLUMNS() / MEASURES() / RELATIONSHIPS()` (falls back
    to the older `INFO.*` functions) gives the full semantic model: tables, columns with their
    descriptions and format strings, measures with their DAX, and declared relationships.
  * rows      — `EVALUATE TOPN(n, 'Table')` per table, materialised into DuckDB so the rest of the
    platform treats the model like any other SQL source.

Auth is either an Entra ID service principal (client credentials) or a pasted access token. The API
base and authority are configurable so sovereign clouds — and the test stub — work unchanged.
"""
from __future__ import annotations

import time
from typing import Any, Iterator

import httpx
import pandas as pd

from backend.connectors.base import ConfigField
from backend.connectors.bi_base import (
    BIConnector,
    SemanticColumn,
    SemanticMeasure,
    SemanticModel,
    SemanticRelationship,
    SemanticTable,
    clean_name,
)
from backend.connectors.registry import register

SCOPE = "https://analysis.windows.net/powerbi/api/.default"
# Power BI caps executeQueries at 100k rows / 1M values per query.
HARD_ROW_CAP = 100_000


def dax_table(name: str) -> str:
    return "'" + str(name).replace("'", "''") + "'"


@register
class PowerBIConnector(BIConnector):
    supported_auth = ("service_account", "oauth_code", "device_code", "credentials")
    type_key = "powerbi"
    display_name = "Power BI semantic model"
    semantic_language = "dax"
    config_fields = [
        ConfigField("dataset_id", "Semantic model (dataset) ID", help="GUID from the model's Settings page or its URL"),
        ConfigField("workspace_id", "Workspace (group) ID", required=False, help="Blank = My workspace"),
        ConfigField("auth_mode", "Authentication", type="select", options=["service_principal", "access_token"], default="service_principal"),
        ConfigField("tenant_id", "Directory (tenant) ID", required=False),
        ConfigField("client_id", "Application (client) ID", required=False),
        ConfigField("client_secret", "Client secret", type="password", required=False),
        ConfigField("access_token", "Access token", type="password", required=False, help="Paste a bearer token instead of using a service principal"),
        ConfigField("api_base", "API base URL", required=False, default="https://api.powerbi.com/v1.0/myorg"),
        ConfigField("authority", "Login authority", required=False, default="https://login.microsoftonline.com"),
        ConfigField("max_rows", "Max rows per table", type="integer", required=False, default=100000),
        ConfigField("include_hidden", "Include hidden tables", type="boolean", required=False, default=False),
    ]

    def __init__(self, source_id: str, config: dict[str, Any]):
        super().__init__(source_id, config)
        # per-instance: a class-level cache would leak tokens between sources using different principals
        self._token_cache: tuple[str, float] | None = None

    # ---------------------------------------------------------------- plumbing
    @property
    def api_base(self) -> str:
        return (self.config.get("api_base") or "https://api.powerbi.com/v1.0/myorg").rstrip("/")

    def _token(self) -> str:
        if (self.config.get("auth_mode") or "service_principal") == "access_token":
            tok = self.config.get("access_token")
            if not tok:
                raise ValueError("Access token authentication selected but no token was provided")
            return tok
        if self._token_cache and self._token_cache[1] > time.time() + 60:
            return self._token_cache[0]
        for key in ("tenant_id", "client_id", "client_secret"):
            if not self.config.get(key):
                raise ValueError(f"Service principal authentication requires {key}")
        authority = (self.config.get("authority") or "https://login.microsoftonline.com").rstrip("/")
        url = f"{authority}/{self.config['tenant_id']}/oauth2/v2.0/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self.config["client_id"],
            "client_secret": self.config["client_secret"],
            "scope": SCOPE,
        }
        with httpx.Client(timeout=30) as c:
            r = c.post(url, data=data)
        if r.status_code >= 400:
            raise RuntimeError(f"Entra ID token request failed ({r.status_code}): {r.text[:300]}")
        payload = r.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("Token endpoint returned no access_token")
        self._token_cache = (token, time.time() + float(payload.get("expires_in", 3600)))
        return token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}

    def _dataset_url(self) -> str:
        ws = self.config.get("workspace_id")
        ds = self.config.get("dataset_id")
        if not ds:
            raise ValueError("dataset_id is required")
        return f"{self.api_base}/groups/{ws}/datasets/{ds}" if ws else f"{self.api_base}/datasets/{ds}"

    def execute_dax(self, dax: str) -> list[dict[str, Any]]:
        """Run a DAX query and return its first result table as a list of row dicts."""
        body = {"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}}
        with httpx.Client(timeout=180) as c:
            r = c.post(f"{self._dataset_url()}/executeQueries", headers=self._headers(), json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"Power BI executeQueries failed ({r.status_code}): {r.text[:400]}")
        data = r.json()
        results = data.get("results") or []
        if not results:
            return []
        if "error" in results[0]:
            raise RuntimeError(f"DAX error: {str(results[0]['error'])[:300]}")
        tables = results[0].get("tables") or []
        return tables[0].get("rows", []) if tables else []

    @staticmethod
    def _rows_to_records(rows: list[dict]) -> list[dict]:
        return [{clean_name(k): v for k, v in row.items()} for row in rows]

    def _info(self, view: str) -> list[dict]:
        """INFO.VIEW.X() where available, else the older INFO.X()."""
        try:
            return self._rows_to_records(self.execute_dax(f"EVALUATE INFO.VIEW.{view}()"))
        except RuntimeError:
            return self._rows_to_records(self.execute_dax(f"EVALUATE INFO.{view}()"))

    # ---------------------------------------------------------------- interface
    def test_connection(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            self.execute_dax('EVALUATE ROW("ok", 1)')
            model = self.fetch_semantic_model()
            return {"ok": True, "message": f"Connected — {model.summary()['tables']} tables, "
                                           f"{model.summary()['measures']} measures, "
                                           f"{model.summary()['relationships']} relationships",
                    "latency_ms": int((time.perf_counter() - t0) * 1000), "model": model.summary()}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)[:500]}

    def list_databases(self) -> list[str]:
        """Workspaces visible to the principal — used when picking a source."""
        try:
            with httpx.Client(timeout=30) as c:
                r = c.get(f"{self.api_base}/groups", headers=self._headers())
            return [g.get("name", g.get("id", "")) for g in (r.json().get("value") or [])]
        except Exception:  # noqa: BLE001
            return []

    def fetch_semantic_model(self) -> SemanticModel:
        include_hidden = bool(self.config.get("include_hidden"))

        def truthy(v: Any) -> bool:
            return str(v).strip().lower() in ("true", "1", "yes")

        tables: dict[str, SemanticTable] = {}
        for row in self._info("TABLES"):
            name = row.get("Name") or row.get("TableName")
            if not name:
                continue
            hidden = truthy(row.get("IsHidden"))
            if hidden and not include_hidden:
                continue
            tables[name] = SemanticTable(name=name, description=row.get("Description") or None, is_hidden=hidden)

        for row in self._info("COLUMNS"):
            tname = row.get("Table") or row.get("TableName")
            cname = row.get("Name") or row.get("ColumnName")
            if not tname or not cname or tname not in tables:
                continue
            if str(cname).startswith("RowNumber-"):  # internal Power BI column
                continue
            tables[tname].columns.append(SemanticColumn(
                name=cname,
                data_type=str(row.get("DataType") or "string").lower(),
                description=row.get("Description") or None,
                is_hidden=truthy(row.get("IsHidden")),
                format_string=row.get("FormatString") or None,
                data_category=row.get("DataCategory") or None,
            ))

        measures = []
        for row in self._info("MEASURES"):
            name = row.get("Name") or row.get("MeasureName")
            expr = row.get("Expression")
            if not name or not expr:
                continue
            measures.append(SemanticMeasure(
                name=name, expression=str(expr).strip(), table=row.get("Table") or row.get("TableName"),
                description=row.get("Description") or None, format_string=row.get("FormatString") or None,
                is_hidden=truthy(row.get("IsHidden")), folder=row.get("DisplayFolder") or None,
            ))

        relationships = []
        for row in self._info("RELATIONSHIPS"):
            ft, fc = row.get("FromTable"), row.get("FromColumn")
            tt, tc = row.get("ToTable"), row.get("ToColumn")
            if not all([ft, fc, tt, tc]):
                continue
            from_card = str(row.get("FromCardinality") or "many").lower()
            to_card = str(row.get("ToCardinality") or "one").lower()
            relationships.append(SemanticRelationship(
                from_table=ft, from_column=fc, to_table=tt, to_column=tc,
                cardinality=f"{'many' if 'many' in from_card else 'one'}_to_{'many' if 'many' in to_card else 'one'}",
                is_active=truthy(row.get("IsActive")) if row.get("IsActive") is not None else True,
            ))

        return SemanticModel(
            name=str(self.config.get("dataset_id") or "Power BI model"),
            system="powerbi", language="dax",
            tables=list(tables.values()), measures=measures, relationships=relationships,
        )

    def load_frames(self) -> Iterator[tuple[str, pd.DataFrame]]:
        model, err = self.semantic_model_safe()
        if model is None:
            raise RuntimeError(f"Could not read the semantic model: {err}")
        limit = min(int(self.config.get("max_rows") or HARD_ROW_CAP), HARD_ROW_CAP)
        for t in model.tables:
            rows = self.execute_dax(f"EVALUATE TOPN({limit}, {dax_table(t.name)})")
            df = pd.DataFrame(self._rows_to_records(rows))
            if df.empty and t.columns:
                df = pd.DataFrame(columns=[c.name for c in t.columns])
            yield t.name, df
