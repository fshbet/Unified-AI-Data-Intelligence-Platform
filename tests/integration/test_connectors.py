"""Connector integration tests. PostgreSQL / Redis / MySQL tests run only when the service is reachable
(set EDI_TEST_PG_URL / EDI_TEST_REDIS_URL, or start docker compose)."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from backend.connectors.registry import create_connector, list_connector_types


def test_registry_lists_all_connectors():
    types = {t["type"] for t in list_connector_types()}
    assert {"postgresql", "mysql", "mssql", "sqlite", "oracle", "duckdb", "file", "redis", "mongodb", "rest_api", "graphql"} <= types


def test_file_connector_csv_excel_json_parquet(tmp_path: Path):
    df = pd.DataFrame({"id": [1, 2, 3], "amount": [10.5, 20.0, 30.25], "when": ["2026-01-01", "2026-02-01", "2026-03-01"]})
    df.to_csv(tmp_path / "a.csv", index=False)
    df.to_parquet(tmp_path / "b.parquet", index=False)
    (tmp_path / "c.json").write_text('{"data": [{"id": 1, "nested": {"x": 1}}, {"id": 2, "nested": {"x": 2}}]}')
    with pd.ExcelWriter(tmp_path / "d.xlsx") as xw:
        df.to_excel(xw, sheet_name="one", index=False)
        df.head(1).to_excel(xw, sheet_name="two", index=False)
    conn = create_connector("file", "t_files", {"files": [str(p) for p in tmp_path.iterdir()]})
    assert conn.test_connection()["ok"]
    names = {t.name for t in conn.list_tables()}
    assert {"a", "b", "c", "d_one", "d_two"} <= names
    schema = conn.get_schema("a")
    assert [c.name for c in schema.columns] == ["id", "amount", "when"] and schema.row_count == 3
    r = conn.execute_query('SELECT SUM(amount) AS s FROM "a"')
    assert r.rows[0][0] == pytest.approx(60.75)
    c = conn.get_schema("c")
    assert "nested_x" in {x.name for x in c.columns} or "nested" in {x.name for x in c.columns}
    assert conn.get_row_count("b") == 3


def test_sqlite_connector(tmp_path: Path):
    p = tmp_path / "x.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE customers (customer_id TEXT PRIMARY KEY, name TEXT)")
    con.execute("CREATE TABLE orders (order_id INTEGER PRIMARY KEY, customer_id TEXT REFERENCES customers(customer_id), amount REAL)")
    con.executemany("INSERT INTO customers VALUES (?,?)", [("C1", "A"), ("C2", "B")])
    con.executemany("INSERT INTO orders VALUES (?,?,?)", [(1, "C1", 5.0), (2, "C2", 7.5)])
    con.commit()
    con.close()
    conn = create_connector("sqlite", "t_sqlite", {"path": str(p)})
    assert conn.test_connection()["ok"]
    assert {t.name for t in conn.list_tables()} == {"customers", "orders"}
    s = conn.get_schema("orders")
    fk = next(c for c in s.columns if c.name == "customer_id")
    assert fk.is_foreign_key and fk.fk_target.endswith("customers.customer_id")
    assert next(c for c in s.columns if c.name == "order_id").is_primary_key
    r = conn.execute_query("SELECT customer_id, SUM(amount) FROM orders GROUP BY 1", limit=10)
    assert len(r.rows) == 2 and not r.truncated
    assert conn.sample_data("customers", n=1).rows[0][0] == "C1"


def test_rest_api_connector_with_local_server():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            page = int((self.path.split("page=")[1] if "page=" in self.path else "1").split("&")[0])
            body = {"data": [{"id": (page - 1) * 2 + i, "v": {"x": i}} for i in range(2)]} if page <= 2 else {"data": []}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *a):  # noqa: D401
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        conn = create_connector("rest_api", "t_api", {"url": f"http://127.0.0.1:{srv.server_port}/api", "method": "GET", "response_path": "data", "pagination": "page", "page_param": "page", "page_size": 2, "table_name": "items"})
        assert conn.test_connection()["ok"]
        conn.refresh()
        s = conn.get_schema("items")
        assert {"id", "v_x"} <= {c.name for c in s.columns} and s.row_count == 4
    finally:
        srv.shutdown()


@pytest.mark.skipif(not os.environ.get("EDI_TEST_PG_URL"), reason="EDI_TEST_PG_URL not set")
def test_postgres_connector():
    from urllib.parse import urlparse

    u = urlparse(os.environ["EDI_TEST_PG_URL"])
    conn = create_connector("postgresql", "t_pg", {"host": u.hostname, "port": u.port or 5432, "database": u.path.lstrip("/"), "username": u.username, "password": u.password})
    assert conn.test_connection()["ok"]
    from sqlalchemy import text

    with conn.engine.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS edi_test_orders"))
        c.execute(text("CREATE TABLE edi_test_orders (id SERIAL PRIMARY KEY, region TEXT, amount NUMERIC, order_date DATE)"))
        c.execute(text("INSERT INTO edi_test_orders (region, amount, order_date) VALUES ('West', 10, '2026-08-01'), ('East', 20, '2026-08-02')"))
    assert "edi_test_orders" in {t.name for t in conn.list_tables()}
    s = conn.get_schema("edi_test_orders", "public")
    assert next(c for c in s.columns if c.name == "id").is_primary_key
    r = conn.execute_query("SELECT region, SUM(amount) FROM edi_test_orders GROUP BY 1 ORDER BY 1")
    assert r.rows == [["East", 20.0], ["West", 10.0]] or [r[0] for r in r.rows] == ["East", "West"]
    with pytest.raises(Exception):
        conn.execute_query("SELECT pg_sleep(5)", timeout=1)


@pytest.mark.skipif(not os.environ.get("EDI_TEST_REDIS_URL"), reason="EDI_TEST_REDIS_URL not set")
def test_redis_connector():
    import redis
    from urllib.parse import urlparse

    u = urlparse(os.environ["EDI_TEST_REDIS_URL"])
    r = redis.Redis(host=u.hostname, port=u.port or 6379, decode_responses=True)
    r.flushdb()
    r.hset("order:1", mapping={"customer_id": "C1", "amount": 10})
    r.hset("order:2", mapping={"customer_id": "C2", "amount": 20})
    r.set("config:site", "prod")
    r.set("session:abc", '{"user": "u1", "ttl": 30}')
    r.lpush("queue:jobs", "a", "b")
    conn = create_connector("redis", "t_redis", {"host": u.hostname, "port": u.port or 6379})
    assert conn.test_connection()["ok"]
    conn.refresh()
    names = {t.name for t in conn.list_tables()}
    assert {"order", "session", "redis_keys"} <= names
    res = conn.execute_query('SELECT SUM(amount) FROM "order"')
    assert res.rows[0][0] == 30
