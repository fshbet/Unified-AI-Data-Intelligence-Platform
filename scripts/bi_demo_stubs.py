"""Local stand-ins for Power BI and Tableau Server, so the BI semantic-model import can be seen
working without a real tenant or server.

They speak the same wire protocols the connectors use in production — Entra ID token +
`executeQueries` for Power BI, REST sign-in + Metadata GraphQL + VizQL Data Service for Tableau —
and serve a small model containing both translatable measures and deliberately untranslatable ones
(`TOTALYTD`, `WINDOW_SUM`) so the definition-only behaviour is visible.

    python scripts/bi_demo_stubs.py            # then add the two sources printed below

The same handlers back tests/integration/test_bi_connectors.py, so what you see here is what the
test suite covers.
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PBI_PORT, TAB_PORT = 8791, 8792

# ---------------------------------------------------------------- Power BI model
PBI_TABLES = [
    {"Name": "Sales", "Description": "Order lines from the ERP", "IsHidden": False},
    {"Name": "Product", "Description": "Product master", "IsHidden": False},
    {"Name": "_Internal", "Description": None, "IsHidden": True},
]
PBI_COLUMNS = [
    {"Table": "Sales", "Name": "OrderId", "DataType": "String", "IsHidden": False},
    {"Table": "Sales", "Name": "ProductKey", "DataType": "String", "IsHidden": False},
    {"Table": "Sales", "Name": "Revenue", "DataType": "Decimal", "Description": "Net revenue for the line", "FormatString": "₹#,0", "IsHidden": False},
    {"Table": "Sales", "Name": "Qty", "DataType": "Int64", "IsHidden": False},
    {"Table": "Sales", "Name": "Status", "DataType": "String", "IsHidden": False},
    {"Table": "Sales", "Name": "RowNumber-2662979B", "DataType": "Int64", "IsHidden": True},
    {"Table": "Product", "Name": "ProductKey", "DataType": "String", "IsHidden": False},
    {"Table": "Product", "Name": "Category", "DataType": "String", "Description": "Merch category", "IsHidden": False},
]
PBI_MEASURES = [
    {"Table": "Sales", "Name": "Total Revenue", "Expression": "SUM('Sales'[Revenue])", "Description": "All revenue", "FormatString": "₹#,0"},
    {"Table": "Sales", "Name": "Posted Revenue", "Expression": "CALCULATE(SUM('Sales'[Revenue]), 'Sales'[Status] = \"POSTED\")"},
    {"Table": "Sales", "Name": "Order Count", "Expression": "COUNTROWS('Sales')"},
    {"Table": "Sales", "Name": "Avg Price", "Expression": "DIVIDE(SUM('Sales'[Revenue]), SUM('Sales'[Qty]))"},
    {"Table": "Sales", "Name": "Revenue YTD", "Expression": "TOTALYTD(SUM('Sales'[Revenue]), 'Date'[Date])"},
    {"Table": "Sales", "Name": "Hidden Helper", "Expression": "SUM('Sales'[Qty])", "IsHidden": True},
]
PBI_RELATIONSHIPS = [
    {"FromTable": "Sales", "FromColumn": "ProductKey", "ToTable": "Product", "ToColumn": "ProductKey",
     "FromCardinality": "Many", "ToCardinality": "One", "IsActive": True},
]
PBI_ROWS = {
    "Sales": [
        {"Sales[OrderId]": "O1", "Sales[ProductKey]": "P1", "Sales[Revenue]": 100.0, "Sales[Qty]": 2, "Sales[Status]": "POSTED"},
        {"Sales[OrderId]": "O2", "Sales[ProductKey]": "P2", "Sales[Revenue]": 250.0, "Sales[Qty]": 5, "Sales[Status]": "POSTED"},
        {"Sales[OrderId]": "O3", "Sales[ProductKey]": "P1", "Sales[Revenue]": 50.0, "Sales[Qty]": 1, "Sales[Status]": "CANCELLED"},
    ],
    "Product": [
        {"Product[ProductKey]": "P1", "Product[Category]": "Hardware"},
        {"Product[ProductKey]": "P2", "Product[Category]": "Software"},
    ],
}

# ---------------------------------------------------------------- Tableau model
TABLEAU_FIELDS = [
    {"__typename": "ColumnField", "name": "Order Date", "dataType": "DATE", "role": "DIMENSION", "isHidden": False},
    {"__typename": "ColumnField", "name": "Region", "dataType": "STRING", "role": "DIMENSION", "isHidden": False,
     "description": "Sales territory"},
    {"__typename": "ColumnField", "name": "Sales Amount", "dataType": "REAL", "role": "MEASURE", "isHidden": False},
    {"__typename": "ColumnField", "name": "Profit", "dataType": "REAL", "role": "MEASURE", "isHidden": False},
    {"__typename": "CalculatedField", "name": "Profit Ratio", "dataType": "REAL", "role": "MEASURE",
     "isHidden": False, "formula": "SUM([Profit]) / SUM([Sales Amount])", "description": "Margin"},
    {"__typename": "CalculatedField", "name": "Running Sales", "dataType": "REAL", "role": "MEASURE",
     "isHidden": False, "formula": "WINDOW_SUM(SUM([Sales Amount]))"},
    {"__typename": "CalculatedField", "name": "Unit Margin", "dataType": "REAL", "role": "MEASURE",
     "isHidden": False, "formula": "[Profit] - [Sales Amount]"},
]
TABLEAU_ROWS = [
    {"Order Date": "2026-08-01", "Region": "West", "Sales Amount": 100.0, "Profit": 20.0},
    {"Order Date": "2026-08-02", "Region": "East", "Sales Amount": 300.0, "Profit": 90.0},
]


class _Json(BaseHTTPRequestHandler):
    def _send(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class PBIHandler(_Json):
    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        if self.path.endswith("/oauth2/v2.0/token"):
            assert "client_credentials" in raw and "client_secret=shh" in raw
            return self._send({"access_token": "stub-token", "expires_in": 3600})
        if self.path.endswith("/executeQueries"):
            if self.headers.get("Authorization") != "Bearer stub-token":
                return self._send({"error": "unauthorized"}, 401)
            dax = json.loads(raw)["queries"][0]["query"]
            rows = self._rows_for(dax)
            if rows is None:
                return self._send({"error": {"code": "Unknown", "message": f"unsupported: {dax}"}}, 400)
            return self._send({"results": [{"tables": [{"rows": rows}]}]})
        self._send({"error": "not found"}, 404)

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/groups"):
            return self._send({"value": [{"id": "ws1", "name": "Finance Workspace"}]})
        self._send({"error": "not found"}, 404)

    @staticmethod
    def _rows_for(dax: str):
        d = dax.strip()
        if d.startswith('EVALUATE ROW("ok"'):
            return [{"[ok]": 1}]
        # this "server" only exposes the modern INFO.VIEW.* surface
        if d == "EVALUATE INFO.VIEW.TABLES()":
            return [{f"INFO.VIEW.TABLES()[{k}]": v for k, v in r.items()} for r in PBI_TABLES]
        if d == "EVALUATE INFO.VIEW.COLUMNS()":
            return [{f"[{k}]": v for k, v in r.items()} for r in PBI_COLUMNS]
        if d == "EVALUATE INFO.VIEW.MEASURES()":
            return [{f"[{k}]": v for k, v in r.items()} for r in PBI_MEASURES]
        if d == "EVALUATE INFO.VIEW.RELATIONSHIPS()":
            return [{f"[{k}]": v for k, v in r.items()} for r in PBI_RELATIONSHIPS]
        for name, rows in PBI_ROWS.items():
            if d.startswith("EVALUATE TOPN(") and f"'{name}'" in d:
                return rows
        return None


class TableauHandler(_Json):
    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        if self.path.endswith("/auth/signin"):
            creds = json.loads(raw)["credentials"]
            assert creds["personalAccessTokenName"] == "edi" and creds["personalAccessTokenSecret"] == "secret"
            return self._send({"credentials": {"token": "tab-token", "site": {"id": "site-1", "contentUrl": ""}}})
        if self.headers.get("X-Tableau-Auth") != "tab-token":
            return self._send({"error": "unauthorized"}, 401)
        if self.path.endswith("/metadata/graphql"):
            q = json.loads(raw)["query"]
            if "filter:" in q:
                return self._send({"data": {"publishedDatasources": [{
                    "name": "Superstore Sales", "luid": "ds-luid-1", "projectName": "Finance",
                    "description": "Certified sales datasource", "hasExtracts": True, "fields": TABLEAU_FIELDS,
                }]}})
            return self._send({"data": {"publishedDatasources": [
                {"name": "Superstore Sales", "luid": "ds-luid-1", "projectName": "Finance"}]}})
        if self.path.endswith("/vizql-data-service/query-datasource"):
            wanted = [f["fieldCaption"] for f in json.loads(raw)["query"]["fields"]]
            return self._send({"data": [{k: r.get(k) for k in wanted} for r in TABLEAU_ROWS]})
        self._send({"error": "not found"}, 404)


def _serve(handler, port):
    HTTPServer(("127.0.0.1", port), handler).serve_forever()


def main() -> None:
    threading.Thread(target=_serve, args=(PBIHandler, PBI_PORT), daemon=True).start()
    threading.Thread(target=_serve, args=(TableauHandler, TAB_PORT), daemon=True).start()
    print(f"""
Power BI + Tableau demo stubs are running.

  Power BI stub : http://127.0.0.1:{PBI_PORT}
  Tableau stub  : http://127.0.0.1:{TAB_PORT}

Add them in the app under Data Sources -> Add data source -> "BI semantic models":

  Power BI semantic model
    Semantic model (dataset) ID   ds-1
    Authentication                service_principal
    Directory (tenant) ID         t1
    Application (client) ID       c1
    Client secret                 shh
    API base URL                  http://127.0.0.1:{PBI_PORT}
    Login authority               http://127.0.0.1:{PBI_PORT}

  Tableau Server / Cloud
    Server URL                    http://127.0.0.1:{TAB_PORT}
    Authentication                pat
    Personal access token name    edi
    Personal access token secret  secret
    Published datasource LUID     ds-luid-1

After syncing, look at Metrics: translated measures show their SQL plus the original DAX/Tableau
formula, while `Revenue YTD` (TOTALYTD) and `Running Sales` (WINDOW_SUM) arrive as
"definition only" — kept verbatim, never given a computed value.

Press Ctrl+C to stop.
""", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
