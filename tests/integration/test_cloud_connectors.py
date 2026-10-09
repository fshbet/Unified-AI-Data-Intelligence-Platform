"""Google Sheets / BigQuery and Microsoft Graph connectors.

Stub servers speaking the real wire protocols rather than mocked HTTP clients — a mock will
happily accept a request shape Google or Graph would reject, which defeats the purpose.

The fixtures deliberately contain the mess real spreadsheets contain: blank header cells,
duplicate headers, trailing spaces, a numeric column with one text value, short rows, and a tab
name with spaces and parentheses.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from backend.auth_providers.base import Credential
from backend.connectors.google_connector import BigQueryConnector, GoogleSheetsConnector, normalise_headers
from backend.connectors.microsoft_connector import ExcelOnlineConnector, SharePointListConnector

CALLS: list[dict] = []
STATE = {"throttle_once": True}

SHEET_VALUES = [
    ["Region", "", "Revenue", "Revenue", " Notes "],          # blank + duplicate + padded
    ["West", "x", 100, 200, "Renewal due"],
    ["East", "y", 300, 400],                                   # short row
    ["North", "z", 500, "n/a", "Mixed column"],                # one text value in a number column
]


class _Stub(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        CALLS.append({"method": "GET", "path": u.path, "query": parse_qs(u.query), "url": self.path})
        p = u.path

        # ---- Google Sheets
        if p.endswith("/spreadsheets/sheet-1"):
            return self._json({"properties": {"title": "Demo"},
                               "sheets": [{"properties": {"title": "Q3 Results (final)"}},
                                          {"properties": {"title": "Ignored"}}]})
        if "/values/" in p:
            return self._json({"values": SHEET_VALUES})
        if p.endswith("/spreadsheets/missing"):
            return self._json({"error": "not found"}, 404)

        # ---- Graph: Excel
        if p.endswith("/workbook/worksheets"):
            return self._json({"value": [{"name": "Budget"}]})
        if p.endswith("/usedRange"):
            return self._json({"values": SHEET_VALUES})

        # ---- Graph: SharePoint lists, with throttling and paging
        if p.endswith("/lists"):
            if STATE["throttle_once"]:
                STATE["throttle_once"] = False
                self.send_response(429)
                self.send_header("Retry-After", "1")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._json({"value": [{"id": "L1", "displayName": "Vendors"}]})
        if p.endswith("/columns"):
            return self._json({"value": [{"name": "Title", "displayName": "Vendor"},
                                         {"name": "OData__x0020_Cost", "displayName": "Cost"}]})
        if p.endswith("/items"):
            if u.query and "skip" in u.query:
                return self._json({"value": [{"fields": {"Title": "Beta", "OData__x0020_Cost": 2}}]})
            host = f"http://127.0.0.1:{self.server.server_port}"
            return self._json({"value": [{"fields": {"Title": "Alpha", "OData__x0020_Cost": 1}}],
                               "@odata.nextLink": f"{host}{p}?skip=opaque%3Atoken&expand=fields"})
        self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        body = json.loads(raw) if raw else {}
        CALLS.append({"method": "POST", "path": urlparse(self.path).path, "body": body})
        sql = body.get("query", "")
        if "INFORMATION_SCHEMA.TABLES" in sql:
            return self._json({"schema": {"fields": [{"name": "table_name"}]},
                               "rows": [{"f": [{"v": "orders"}]}]})
        return self._json({"schema": {"fields": [{"name": "id"}, {"name": "amount"}]},
                           "rows": [{"f": [{"v": "1"}, {"v": "10"}]},
                                    {"f": [{"v": "2"}, {"v": "20"}]}]})

    def _json(self, payload, code=200):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def stub():
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def _reset():
    CALLS.clear()
    STATE["throttle_once"] = True


def _cred(**meta) -> Credential:
    return Credential(kind="bearer", secret="tok", metadata=meta)


# ---------------------------------------------------------------- header handling
def test_header_normalisation_handles_what_real_sheets_contain():
    out = normalise_headers(["Region", "", "Revenue", "Revenue", " Notes ", None])
    # A blank header must be NAMED, not dropped — dropping it shifts every later column left.
    assert out == ["Region", "column_2", "Revenue", "Revenue_2", "Notes", "column_6"]


# ---------------------------------------------------------------- google sheets
def test_sheets_reads_rows_and_survives_messy_data(stub):
    c = GoogleSheetsConnector("s1", {"spreadsheet_id": "sheet-1", "api_base": f"{stub}/spreadsheets",
                                     "sheets": "Q3 Results (final)", "_credential": _cred()})
    assert c.test_connection()["ok"]
    name, df = next(iter(c.load_frames()))
    assert name == "q3_results_final", "a tab name with spaces/brackets must become a usable identifier"
    assert list(df.columns) == ["Region", "column_2", "Revenue", "Revenue_2", "Notes"]
    assert len(df) == 3
    assert df["Revenue"].tolist() == [100, 300, 500], "a clean numeric column must be typed"
    assert df["Revenue_2"].astype(str).tolist() == ["200", "400", "n/a"], "one bad value keeps the column text"
    import pandas as pd
    # The short row must pad on the RIGHT: "East" keeps its own values and gains a null
    # Note, rather than every later column sliding left by one.
    assert df["Region"].tolist() == ["West", "East", "North"]
    assert pd.isna(df["Notes"].iloc[1])
    assert df["Notes"].iloc[0] == "Renewal due"


def test_sheets_requests_unformatted_values(stub):
    """Without this numbers arrive as display strings like '1,234.50'."""
    c = GoogleSheetsConnector("s2", {"spreadsheet_id": "sheet-1", "api_base": f"{stub}/spreadsheets",
                                     "_credential": _cred()})
    list(c.load_frames())
    values_call = next(c for c in CALLS if "/values/" in c["path"])
    assert values_call["query"]["valueRenderOption"] == ["UNFORMATTED_VALUE"]


def test_sheets_404_explains_service_account_sharing(stub):
    """The single most common Google setup failure deserves a usable message."""
    c = GoogleSheetsConnector("s3", {"spreadsheet_id": "missing", "api_base": f"{stub}/spreadsheets",
                                     "_credential": _cred(client_email="sync@proj.iam.gserviceaccount.com")})
    res = c.test_connection()
    assert not res["ok"]
    assert "sync@proj.iam.gserviceaccount.com" in res["message"] and "shared" in res["message"]


def test_connector_without_a_credential_fails_clearly(stub):
    c = GoogleSheetsConnector("s4", {"spreadsheet_id": "sheet-1", "api_base": f"{stub}/spreadsheets"})
    assert "authorise" in c.test_connection()["message"]


# ---------------------------------------------------------------- bigquery
def test_bigquery_always_caps_bytes_billed_and_refuses_writes(stub):
    c = BigQueryConnector("b1", {"project_id": "p", "dataset_id": "d", "api_base": stub,
                                 "max_bytes_billed": 5_000_000, "_credential": _cred()})
    frames = dict(c.load_frames())
    assert frames["orders"]["amount"].tolist() == [10, 20]
    posts = [x for x in CALLS if x["method"] == "POST"]
    assert posts and all(p["body"]["maximumBytesBilled"] == "5000000" for p in posts), \
        "an uncapped query against a billed API is a financial incident waiting to happen"

    with pytest.raises(Exception, match="(?i)select|read-only|not allowed"):
        c._query("DROP TABLE d.orders")


# ---------------------------------------------------------------- microsoft graph
def test_excel_reads_used_range_with_the_same_header_rules(stub):
    c = ExcelOnlineConnector("m1", {"item_path": "/Finance/Budget.xlsx", "api_base": stub,
                                    "_credential": _cred()})
    assert c.test_connection()["ok"]
    name, df = next(iter(c.load_frames()))
    assert name == "budget"
    assert list(df.columns) == ["Region", "column_2", "Revenue", "Revenue_2", "Notes"]
    assert df["Revenue"].tolist() == [100, 300, 500]


def test_sharepoint_follows_nextlink_verbatim_and_maps_field_names(stub):
    c = SharePointListConnector("m2", {"site_id": "site-1", "api_base": stub, "_credential": _cred()})
    name, df = next(iter(c.load_frames()))
    assert name == "vendors"
    # Internal names are unusable in a prompt or a UI.
    assert list(df.columns) == ["Vendor", "Cost"]
    assert df["Vendor"].tolist() == ["Alpha", "Beta"], "the second page must be fetched"
    follow = [x for x in CALLS if x["method"] == "GET" and "skip" in x["url"]]
    assert follow, "the @odata.nextLink was never followed"
    assert "skip=opaque%3Atoken" in follow[0]["url"], "the opaque token must be passed through untouched"


def test_graph_respects_retry_after(stub):
    """Ignoring Retry-After gets the whole tenant throttled."""
    c = SharePointListConnector("m3", {"site_id": "site-1", "api_base": stub, "_credential": _cred()})
    res = c.test_connection()
    assert res["ok"], res
    assert STATE["throttle_once"] is False, "the 429 path was never exercised"


def test_connectors_declare_their_auth_modes():
    assert GoogleSheetsConnector.supported_auth == ("service_account", "oauth_code", "ambient")
    assert "device_code" in ExcelOnlineConnector.supported_auth
