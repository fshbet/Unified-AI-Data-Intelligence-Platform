"""REST / GraphQL API sources. Fetches JSON (with pagination), extracts the response path, flattens
and materialises into DuckDB. MongoDB lives here too since it is the same "documents → table" shape."""
from __future__ import annotations

import json
from typing import Any, Iterator

import httpx
import pandas as pd

from backend.connectors.base import ConfigField
from backend.connectors.materialized import MaterializedConnector
from backend.connectors.redis_connector import _flatten
from backend.connectors.registry import register


def extract_path(data: Any, path: str | None) -> Any:
    if not path:
        return data
    for part in path.strip().strip(".").split("."):
        if isinstance(data, dict):
            data = data.get(part)
        elif isinstance(data, list) and part.isdigit():
            data = data[int(part)]
        else:
            return None
    return data


@register
class RestAPIConnector(MaterializedConnector):
    type_key = "rest_api"
    display_name = "REST API"
    category = "api"
    config_fields = [
        ConfigField("url", "API URL"),
        ConfigField("method", "Method", type="select", options=["GET", "POST"], default="GET"),
        ConfigField("auth_type", "Authentication", type="select", options=["none", "bearer", "basic", "api_key_header"], default="none", required=False),
        ConfigField("token", "Token / API key", type="password", required=False),
        ConfigField("username", "Username (basic)", required=False),
        ConfigField("password", "Password (basic)", type="password", required=False),
        ConfigField("api_key_header", "API key header name", required=False, default="X-API-Key"),
        ConfigField("headers", "Extra headers (JSON)", type="textarea", required=False),
        ConfigField("params", "Query params (JSON)", type="textarea", required=False),
        ConfigField("body", "Request body (JSON, POST)", type="textarea", required=False),
        ConfigField("response_path", "Response path", required=False, help="e.g. data.items"),
        ConfigField("table_name", "Table name", required=False, default="api_data"),
        ConfigField("pagination", "Pagination", type="select", options=["none", "page", "offset", "cursor"], default="none", required=False),
        ConfigField("page_param", "Page/offset param", required=False, default="page"),
        ConfigField("page_size_param", "Page size param", required=False),
        ConfigField("page_size", "Page size", type="integer", required=False, default=100),
        ConfigField("cursor_path", "Next cursor path", required=False, help="e.g. meta.next_cursor"),
        ConfigField("max_pages", "Max pages", type="integer", required=False, default=50),
    ]

    def _request_kwargs(self) -> dict[str, Any]:
        c = self.config
        headers = json.loads(c["headers"]) if c.get("headers") else {}
        auth = None
        if c.get("auth_type") == "bearer" and c.get("token"):
            headers["Authorization"] = f"Bearer {c['token']}"
        elif c.get("auth_type") == "api_key_header" and c.get("token"):
            headers[c.get("api_key_header") or "X-API-Key"] = c["token"]
        elif c.get("auth_type") == "basic":
            auth = (c.get("username", ""), c.get("password", ""))
        return {"headers": headers, "auth": auth}

    def fetch_all(self) -> list[dict]:
        c = self.config
        kw = self._request_kwargs()
        params = json.loads(c["params"]) if c.get("params") else {}
        body = json.loads(c["body"]) if c.get("body") else None
        records: list[dict] = []
        pagination = c.get("pagination") or "none"
        page, cursor = 1, None
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            for _ in range(int(c.get("max_pages") or 50)):
                p = dict(params)
                if pagination == "page":
                    p[c.get("page_param") or "page"] = page
                elif pagination == "offset":
                    p[c.get("page_param") or "offset"] = (page - 1) * int(c.get("page_size") or 100)
                elif pagination == "cursor" and cursor:
                    p[c.get("page_param") or "cursor"] = cursor
                if c.get("page_size_param"):
                    p[c["page_size_param"]] = int(c.get("page_size") or 100)
                resp = client.request(c.get("method") or "GET", c["url"], params=p, json=body, **kw)
                resp.raise_for_status()
                data = resp.json()
                chunk = extract_path(data, c.get("response_path"))
                if isinstance(chunk, dict):
                    chunk = [chunk]
                if not chunk:
                    break
                records.extend(chunk)
                if pagination == "none":
                    break
                if pagination == "cursor":
                    cursor = extract_path(data, c.get("cursor_path"))
                    if not cursor:
                        break
                elif len(chunk) < int(c.get("page_size") or 100):
                    break
                page += 1
        return records

    def test_connection(self) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                resp = client.request(self.config.get("method") or "GET", self.config["url"], **self._request_kwargs())
            return {"ok": resp.status_code < 400, "message": f"HTTP {resp.status_code}", "status_code": resp.status_code}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}

    def load_frames(self) -> Iterator[tuple[str, pd.DataFrame]]:
        rows = [_flatten(r) if isinstance(r, dict) else {"value": r} for r in self.fetch_all()]
        yield (self.config.get("table_name") or "api_data"), pd.DataFrame(rows)


@register
class GraphQLConnector(RestAPIConnector):
    type_key = "graphql"
    display_name = "GraphQL API"
    category = "api"
    config_fields = [
        ConfigField("url", "GraphQL endpoint"),
        ConfigField("query", "Query", type="textarea"),
        ConfigField("variables", "Variables (JSON)", type="textarea", required=False),
        ConfigField("token", "Bearer token", type="password", required=False),
        ConfigField("response_path", "Response path", required=False, help="e.g. data.orders.nodes"),
        ConfigField("table_name", "Table name", required=False, default="graphql_data"),
    ]

    def fetch_all(self) -> list[dict]:
        c = self.config
        headers = {"Authorization": f"Bearer {c['token']}"} if c.get("token") else {}
        body = {"query": c["query"], "variables": json.loads(c["variables"]) if c.get("variables") else {}}
        with httpx.Client(timeout=60) as client:
            resp = client.post(c["url"], json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        chunk = extract_path(data, c.get("response_path") or "data")
        if isinstance(chunk, dict):  # take the first list found under data
            for v in chunk.values():
                if isinstance(v, list):
                    return v
            return [chunk]
        return chunk or []


@register
class MongoDBConnector(MaterializedConnector):
    type_key = "mongodb"
    display_name = "MongoDB"
    category = "nosql"
    config_fields = [
        ConfigField("uri", "Connection URI", default="mongodb://localhost:27017"),
        ConfigField("database", "Database"),
        ConfigField("collections", "Collections (comma-separated, blank = all)", required=False),
        ConfigField("sample_limit", "Max documents per collection", type="integer", required=False, default=100000),
    ]

    def _db(self):
        from pymongo import MongoClient

        return MongoClient(self.config["uri"], serverSelectionTimeoutMS=5000)[self.config["database"]]

    def test_connection(self) -> dict[str, Any]:
        try:
            names = self._db().list_collection_names()
            return {"ok": True, "message": f"{len(names)} collections"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}

    def load_frames(self) -> Iterator[tuple[str, pd.DataFrame]]:
        db = self._db()
        wanted = [c.strip() for c in (self.config.get("collections") or "").split(",") if c.strip()]
        limit = int(self.config.get("sample_limit") or 100000)
        for name in wanted or db.list_collection_names():
            docs = []
            for d in db[name].find().limit(limit):
                d["_id"] = str(d.get("_id"))
                docs.append(_flatten(d))
            if docs:
                yield name, pd.DataFrame(docs)
