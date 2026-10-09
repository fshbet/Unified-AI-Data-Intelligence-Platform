"""Tableau Server / Tableau Cloud connector (works against a local on-prem server).

Three official APIs are used, all over plain HTTPS:

  * REST `auth/signin`        — personal access token or username/password, per site.
  * Metadata API (GraphQL)    — the published datasource's fields, descriptions, folders and
    calculated-field formulas. This is Tableau's semantic model and maps onto our catalog directly.
  * VizQL Data Service        — `query-datasource` returns the datasource's rows, which are
    materialised into DuckDB. Servers older than 2024.2 do not have it; the connector then imports
    metadata only and says so rather than pretending the source is empty.

A published datasource is flat from a consumer's point of view, so it becomes one table in our
catalog. Calculated fields that aggregate become measures; row-level calculations stay columns,
because VizQL returns their computed values with the data.
"""
from __future__ import annotations

import re
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
    SemanticTable,
)
from backend.connectors.registry import register
from backend.semantic.expressions import AGG_MARKERS

DATASOURCE_QUERY = """
query getDatasource {
  publishedDatasources(filter: {%FILTER%}) {
    name
    luid
    description
    projectName
    hasExtracts
    fields {
      __typename
      name
      description
      isHidden
      folderName
      ... on CalculatedField { formula dataType role aggregation }
      ... on ColumnField { dataType role aggregation }
      ... on DatasourceField { dataType role aggregation }
    }
  }
}
"""


def slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", str(name)).strip("_").lower() or "datasource"
    return ("t_" + s) if s[0].isdigit() else s


@register
class TableauConnector(BIConnector):
    supported_auth = ("credentials",)  # PAT or username/password; Tableau has no SP flow
    type_key = "tableau"
    display_name = "Tableau Server / Cloud"
    semantic_language = "tableau"
    config_fields = [
        ConfigField("server_url", "Server URL", help="e.g. https://tableau.company.local"),
        ConfigField("site_content_url", "Site content URL", required=False, default="", help="Blank = Default site"),
        ConfigField("auth_mode", "Authentication", type="select", options=["pat", "password"], default="pat"),
        ConfigField("pat_name", "Personal access token name", required=False),
        ConfigField("pat_secret", "Personal access token secret", type="password", required=False),
        ConfigField("username", "Username", required=False),
        ConfigField("password", "Password", type="password", required=False),
        ConfigField("datasource_luid", "Published datasource LUID", required=False, help="Either the LUID or the name below"),
        ConfigField("datasource_name", "Published datasource name", required=False),
        ConfigField("api_version", "REST API version", required=False, default="3.22"),
        ConfigField("max_rows", "Max rows", type="integer", required=False, default=200000),
        ConfigField("metadata_only", "Metadata only (skip row data)", type="boolean", required=False, default=False),
        ConfigField("verify_ssl", "Verify TLS certificate", type="boolean", required=False, default=True,
                    help="Turn off only for a local server using a self-signed certificate"),
    ]

    _auth: tuple[str, str] | None = None  # (token, site_id)

    # ---------------------------------------------------------------- plumbing
    @property
    def server(self) -> str:
        url = (self.config.get("server_url") or "").rstrip("/")
        if not url:
            raise ValueError("server_url is required")
        return url

    def _client(self, timeout: int = 60) -> httpx.Client:
        return httpx.Client(timeout=timeout, verify=bool(self.config.get("verify_ssl", True)))

    def signin(self) -> tuple[str, str]:
        if self._auth:
            return self._auth
        site = {"contentUrl": self.config.get("site_content_url") or ""}
        if (self.config.get("auth_mode") or "pat") == "pat":
            if not self.config.get("pat_name") or not self.config.get("pat_secret"):
                raise ValueError("Personal access token authentication requires pat_name and pat_secret")
            creds = {"personalAccessTokenName": self.config["pat_name"],
                     "personalAccessTokenSecret": self.config["pat_secret"], "site": site}
        else:
            if not self.config.get("username") or not self.config.get("password"):
                raise ValueError("Password authentication requires username and password")
            creds = {"name": self.config["username"], "password": self.config["password"], "site": site}
        url = f"{self.server}/api/{self.config.get('api_version') or '3.22'}/auth/signin"
        with self._client(30) as c:
            r = c.post(url, json={"credentials": creds}, headers={"Accept": "application/json"})
        if r.status_code >= 400:
            raise RuntimeError(f"Tableau sign-in failed ({r.status_code}): {r.text[:300]}")
        payload = (r.json() or {}).get("credentials") or {}
        token = payload.get("token")
        if not token:
            raise RuntimeError("Tableau sign-in returned no token")
        self._auth = (token, (payload.get("site") or {}).get("id", ""))
        return self._auth

    def _headers(self) -> dict[str, str]:
        token, _ = self.signin()
        return {"X-Tableau-Auth": token, "Content-Type": "application/json", "Accept": "application/json"}

    def graphql(self, query: str) -> dict[str, Any]:
        with self._client(120) as c:
            r = c.post(f"{self.server}/api/metadata/graphql", headers=self._headers(), json={"query": query})
        if r.status_code >= 400:
            raise RuntimeError(f"Tableau Metadata API failed ({r.status_code}): {r.text[:300]}")
        data = r.json() or {}
        if data.get("errors"):
            raise RuntimeError(f"Metadata API error: {str(data['errors'])[:300]}")
        return data.get("data") or {}

    def _datasource(self) -> dict[str, Any]:
        luid, name = self.config.get("datasource_luid"), self.config.get("datasource_name")
        if luid:
            filt = f'luid: "{luid}"'
        elif name:
            filt = f'name: "{name}"'
        else:
            raise ValueError("Either datasource_luid or datasource_name is required")
        data = self.graphql(DATASOURCE_QUERY.replace("%FILTER%", filt))
        nodes = data.get("publishedDatasources") or []
        if not nodes:
            raise RuntimeError("No published datasource matched — check the LUID/name and the site")
        return nodes[0]

    # ---------------------------------------------------------------- interface
    def test_connection(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            self.signin()
            model = self.fetch_semantic_model()
            s = model.summary()
            return {"ok": True, "message": f"Connected — datasource '{model.name}': {s['columns']} fields, "
                                           f"{s['measures']} calculated measures",
                    "latency_ms": int((time.perf_counter() - t0) * 1000), "model": s}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)[:500]}

    def list_databases(self) -> list[str]:
        """Published datasource names on the site."""
        try:
            data = self.graphql("query { publishedDatasources { name luid projectName } }")
            return [f"{d.get('projectName', '')}/{d.get('name', '')}".strip("/")
                    for d in (data.get("publishedDatasources") or [])]
        except Exception:  # noqa: BLE001
            return []

    def fetch_semantic_model(self) -> SemanticModel:
        ds = self._datasource()
        table_name = slug(ds.get("name") or "datasource")
        columns: list[SemanticColumn] = []
        measures: list[SemanticMeasure] = []

        for f in ds.get("fields") or []:
            fname = f.get("name")
            if not fname:
                continue
            formula = f.get("formula")
            is_calc = f.get("__typename") == "CalculatedField" and formula
            # an aggregating calculation is a measure; a row-level one is just a derived column
            if is_calc and AGG_MARKERS.search(str(formula)):
                measures.append(SemanticMeasure(
                    name=fname, expression=str(formula).strip(), table=table_name,
                    description=f.get("description") or None, is_hidden=bool(f.get("isHidden")),
                    folder=f.get("folderName") or None,
                ))
                continue
            columns.append(SemanticColumn(
                name=fname,
                data_type=str(f.get("dataType") or "string").lower(),
                description=f.get("description") or None,
                is_hidden=bool(f.get("isHidden")),
                data_category=(f.get("role") or None),
            ))

        table = SemanticTable(name=table_name, description=ds.get("description") or None, columns=columns)
        return SemanticModel(
            name=ds.get("name") or table_name, system="tableau", language="tableau",
            description=ds.get("description") or None, tables=[table], measures=measures, relationships=[],
        )

    def query_datasource(self, luid: str, fields: list[str], limit: int) -> list[dict]:
        body = {
            "datasource": {"datasourceLuid": luid},
            "query": {"fields": [{"fieldCaption": f} for f in fields]},
            "options": {"returnFormat": "OBJECTS", "debug": False},
        }
        with self._client(180) as c:
            r = c.post(f"{self.server}/api/v1/vizql-data-service/query-datasource",
                       headers=self._headers(), json=body)
        if r.status_code == 404:
            raise RuntimeError("VizQL Data Service is not available on this server (needs Tableau 2024.2+). "
                               "Enable 'Metadata only' to import the semantic model without row data.")
        if r.status_code >= 400:
            raise RuntimeError(f"VizQL Data Service query failed ({r.status_code}): {r.text[:300]}")
        data = r.json() or {}
        return (data.get("data") or [])[:limit]

    def load_frames(self) -> Iterator[tuple[str, pd.DataFrame]]:
        ds = self._datasource()
        model = self.fetch_semantic_model()
        table = model.tables[0]
        if self.config.get("metadata_only"):
            yield table.name, pd.DataFrame(columns=[c.name for c in table.columns])
            return
        fields = [c.name for c in table.columns if not c.is_hidden]
        if not fields:
            yield table.name, pd.DataFrame()
            return
        rows = self.query_datasource(ds.get("luid") or self.config.get("datasource_luid", ""),
                                     fields, int(self.config.get("max_rows") or 200000))
        df = pd.DataFrame(rows)
        if df.empty:
            df = pd.DataFrame(columns=fields)
        yield table.name, df
