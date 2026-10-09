"""Embeddable client for the unified API.

Copy this one file into any Python application. The only dependency is `httpx`.

    from edi_client import EDI

    edi = EDI("http://localhost:8010", api_key="edi_live_...")

    for record in edi.records(entity="sales"):        # pages automatically
        print(record.fields["revenue"], record.source.name)

    answer = edi.ask("why did revenue decline in August?")
    print(answer.text)
    print(answer.anonymization)   # proof the model saw tokens, not real values

Paging is handled inside `records()`, so the caller never deals with cursors. That is the whole
point of the contract: attach, iterate, done, regardless of how many systems are behind it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx

__all__ = ["EDI", "Record", "Source", "Answer", "EDIError"]


class EDIError(RuntimeError):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.status = code, status


@dataclass(frozen=True)
class Source:
    connection_id: str
    name: str
    type: str


@dataclass(frozen=True)
class Record:
    id: str
    entity: str
    fields: dict[str, Any]
    source: Source
    lineage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Answer:
    text: str
    conversation_id: str | None
    evidence: list[dict]
    queries: list[dict]
    confidence: dict | None
    anonymization: dict | None
    sources: list[dict]
    warnings: list[str]


class EDI:
    def __init__(self, base_url: str, api_key: str, timeout: float = 120.0) -> None:
        self.base = base_url.rstrip("/") + "/api/v1/unified"
        self._client = httpx.Client(
            timeout=timeout, headers={"Authorization": f"Bearer {api_key}",
                                      "Accept": "application/json"})

    # ------------------------------------------------------------------ plumbing
    def _request(self, method: str, path: str, **kw) -> dict:
        r = self._client.request(method, f"{self.base}{path}", **kw)
        if r.status_code >= 400:
            body = _safe_json(r)
            detail = body.get("detail", body)
            err = detail.get("error", {}) if isinstance(detail, dict) else {}
            raise EDIError(err.get("code", "http_error"),
                           err.get("message", r.text[:200]), r.status_code)
        return r.json()

    # ------------------------------------------------------------------ api
    def schema(self) -> dict:
        """Every entity, table and column this key can reach, with its source."""
        return self._request("GET", "/schema")["data"]

    def sources(self) -> list[dict]:
        """Contributing systems and how fresh each one is."""
        return self._request("GET", "/schema")["sources"]

    def records(self, *, entity: str | None = None, source: str | None = None,
                table: str | None = None, search: str | None = None,
                limit: int = 500, page_size: int = 100) -> Iterator[Record]:
        """Yield records, following cursors until `limit` is reached or the data runs out."""
        cursor, yielded = None, 0
        while yielded < limit:
            params = {"limit": min(page_size, limit - yielded)}
            for k, v in (("entity", entity), ("source", source), ("table", table),
                         ("search", search), ("cursor", cursor)):
                if v:
                    params[k] = v
            body = self._request("GET", "/records", params=params)
            for raw in body["data"]["records"]:
                src = raw.get("source") or {}
                yield Record(id=raw["id"], entity=raw["entity"], fields=raw["fields"],
                             source=Source(src.get("connection_id", ""), src.get("name", ""), src.get("type", "")),
                             lineage=raw.get("lineage", {}))
                yielded += 1
            cursor = (body.get("page") or {}).get("next_cursor")
            if not cursor:
                return

    def ask(self, question: str, conversation_id: str | None = None) -> Answer:
        body = self._request("POST", "/ask", json={"question": question,
                                                   "conversation_id": conversation_id})
        d = body["data"]
        return Answer(text=d.get("answer", ""), conversation_id=d.get("conversation_id"),
                      evidence=d.get("evidence", []), queries=d.get("queries", []),
                      confidence=d.get("confidence"), anonymization=d.get("anonymization"),
                      sources=body.get("sources", []), warnings=body.get("warnings", []))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "EDI":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _safe_json(r: httpx.Response) -> dict:
    try:
        return r.json()
    except ValueError:
        return {}
