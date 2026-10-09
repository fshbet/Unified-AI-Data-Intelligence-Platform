"""The unified output API — one contract any external application attaches to.

Four endpoints, one envelope. Whatever is behind this (SQL databases, files, Sheets, BigQuery,
Graph, Power BI, Tableau), a consumer writes one parser and gets one shape, with provenance on
every record and a `sources` block that makes staleness visible rather than silent.

Authenticated with API keys rather than user JWTs, because the caller is a program. A key acts
with a real user's permissions, so row-level security and PII masking still apply — the unified
API is a different door into the same house, not a way round the locks.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.audit.service import audit
from backend.core.db import get_db
from backend.metadata.catalog import connector_for, source_freshness
from backend.metadata.models import ApiKey, Column, DataSource, Dataset, Table, User, now
from backend.query_engine.executor import execute
from backend.security.access import build_access_context
from backend.security.auth import require_role

router = APIRouter(prefix="/v1/unified", tags=["unified"])

KEY_PREFIX = "edi_live_"
_RATE: dict[str, deque] = defaultdict(deque)


# ---------------------------------------------------------------------------- envelope
def envelope(data: Any, *, page: dict | None = None, sources: list[dict] | None = None,
             warnings: list[str] | None = None, request_id: str | None = None) -> dict:
    """Every response has this shape, successes and failures alike, so a consumer writes one parser."""
    return {
        "ok": True,
        "request_id": request_id or secrets.token_hex(8),
        "data": data,
        "page": page,
        "sources": sources or [],
        "warnings": warnings or [],
    }


def error_envelope(code: str, message: str, status: int = 400) -> HTTPException:
    # Identical key set to a success, or the "one parser" promise is not true.
    return HTTPException(status, {"ok": False, "request_id": secrets.token_hex(8),
                                  "error": {"code": code, "message": message},
                                  "data": None, "page": None, "sources": [], "warnings": []})


# ---------------------------------------------------------------------------- auth
def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def require_api_key(request: Request, db: Session = Depends(get_db)) -> tuple[ApiKey, User]:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise error_envelope("unauthorized", "Supply an API key as 'Authorization: Bearer <key>'", 401)
    raw = header[7:].strip()
    key = db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_key(raw)))
    if key is None or key.revoked_at is not None:
        raise error_envelope("unauthorized", "Unknown or revoked API key", 401)
    if key.expires_at and _aware(key.expires_at) < datetime.now(timezone.utc):
        raise error_envelope("unauthorized", "This API key has expired", 401)

    bucket = _RATE[key.id]
    cutoff = time.monotonic() - 60
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= key.rate_limit_per_min:
        raise HTTPException(429, {"ok": False, "request_id": secrets.token_hex(8),
                                  "error": {"code": "rate_limited",
                                            "message": f"Limit is {key.rate_limit_per_min} requests per minute"},
                                  "data": None, "page": None, "sources": [], "warnings": []},
                            headers={"Retry-After": "60"})
    bucket.append(time.monotonic())

    user = db.get(User, key.user_id) if key.user_id else None
    if user is None or not user.is_active:
        raise error_envelope("unauthorized", "The user this key acts as is inactive", 401)
    key.last_used_at = now()
    db.commit()
    return key, user


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------- scoping
def _visible_tables(db: Session, key: ApiKey, ctx) -> list[Table]:
    """Tables this key may see. Applied here, before any query is built, so a restricted key
    cannot infer counts for data it has no access to."""
    q = select(Table).options(selectinload(Table.dataset).selectinload(Dataset.source),
                              selectinload(Table.columns))
    out = []
    for t in db.scalars(q).all():
        src = t.dataset.source if t.dataset else None
        if src is None or not src.is_enabled:
            continue
        if key.sources and src.name not in key.sources and src.id not in key.sources:
            continue
        if key.entities and _entity_of(t) not in key.entities:
            continue
        if not ctx.can_see_table(t.id):
            continue
        out.append(t)
    return out


def _entity_of(t: Table) -> str:
    return (t.business_domain or "other").lower()


def _source_block(tables: list[Table]) -> list[dict]:
    seen: dict[str, dict] = {}
    for t in tables:
        src = t.dataset.source
        if src.id in seen:
            seen[src.id]["table_count"] += 1
            continue
        fresh = source_freshness(src)
        seen[src.id] = {
            "connection_id": src.id, "name": src.name, "type": src.type,
            "last_sync_at": src.last_sync_at.isoformat() if src.last_sync_at else None,
            "freshness": fresh.get("label"),
            # A consumer must be able to see that one of its five systems is days behind.
            "stale": bool(fresh.get("stale")), "status": src.status, "table_count": 1,
        }
    return list(seen.values())


# ---------------------------------------------------------------------------- schema
@router.get("/schema")
def unified_schema(dep=Depends(require_api_key), db: Session = Depends(get_db)):
    key, user = dep
    ctx = build_access_context(db, user)
    tables = _visible_tables(db, key, ctx)
    data = {"entities": sorted({_entity_of(t) for t in tables}), "tables": []}
    for t in tables:
        cols = [{"name": c.column_name, "type": c.logical_type,
                 "description": c.business_definition or c.description,
                 "sensitive": c.sensitivity in {"pii", "restricted"},
                 "masked": ctx.should_mask(t, c)}
                for c in ctx.visible_columns(t)]
        data["tables"].append({
            "table": t.qualified_name, "entity": _entity_of(t),
            "source": t.dataset.source.name, "connection_id": t.dataset.source.id,
            "description": t.description, "row_count": t.row_count,
            "date_column": t.date_column, "columns": cols,
        })
    return envelope(data, sources=_source_block(tables))


# ---------------------------------------------------------------------------- records
def _encode_cursor(offset: int) -> str:
    # Opaque on purpose: a consumer that sees an integer will start constructing its own.
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).rstrip(b"=").decode()


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        pad = cursor + "=" * (-len(cursor) % 4)
        return int(json.loads(base64.urlsafe_b64decode(pad))["o"])
    except Exception as exc:  # noqa: BLE001
        raise error_envelope("bad_cursor", "That cursor is not valid; omit it to start again") from exc


def _collect(db: Session, key: ApiKey, user: User, *, entity: str | None, source: str | None,
             table: str | None, search: str | None, limit: int, offset: int) -> tuple[list[dict], list[Table], list[str], int]:
    """Gather one page across every table the key can see.

    The page is allocated EVENLY across tables rather than filled table by table. Filling in
    order means a consumer asking for 100 records gets 100 rows from whichever source sorts
    first and reasonably concludes the others are missing — which defeats the entire point of a
    unified feed. With an even allocation, every page is representative of the whole estate, and
    `offset` walks each table forward in step.
    """
    ctx = build_access_context(db, user)
    tables = _visible_tables(db, key, ctx)
    if entity:
        tables = [t for t in tables if _entity_of(t) == entity.lower()]
    if source:
        tables = [t for t in tables if t.dataset.source.name.lower() == source.lower() or t.dataset.source.id == source]
    if table:
        tables = [t for t in tables if table.lower() in {t.table_name.lower(), (t.qualified_name or "").lower()}]
    if not tables:
        return [], [], [], 0

    tables = sorted(tables, key=lambda x: x.qualified_name or x.table_name)
    per_table = max(1, limit // len(tables))
    records: list[dict] = []
    warnings: list[str] = []
    total = sum(t.row_count or 0 for t in tables)

    for t in tables:
        if len(records) >= limit:
            break
        conn_src = t.dataset.source
        cols = [c.column_name for c in ctx.visible_columns(t)]
        if not cols:
            continue
        conn = connector_for(conn_src, db)
        try:
            # quote_ident doubles embedded quotes. Building the identifier with a bare f-string
            # let a column named 'a" , (SELECT ...) AS "b' break out — and column names are NOT
            # sanitised on ingest, so anyone able to add a column to a connected sheet could
            # inject SQL (second-order).
            quoted = ", ".join(conn.quote_ident(c) for c in cols)
            sql = f"SELECT {quoted} FROM {conn.quote_table(t.table_name, t.schema_name)}"
            if search:
                sql += " WHERE " + " OR ".join(
                    f"CAST({conn.quote_ident(c)} AS VARCHAR) ILIKE '%{search}%'" for c in cols)
        finally:
            conn.close()
        want = min(per_table, limit - len(records))
        try:
            rec = execute(db, ctx, conn_src, sql, purpose="unified api", limit=offset + want)
        except Exception as exc:  # noqa: BLE001 - one broken source must not fail the whole call
            # Reported, never silently dropped: a shrinking result with no explanation is worse
            # than an error.
            warnings.append(f"Source '{conn_src.name}' could not be read: {type(exc).__name__}: {exc}"[:300])
            continue
        if rec.error:
            warnings.append(f"Source '{conn_src.name}' returned an error: {rec.error}"[:300])
            continue
        rows = (rec.result.rows if rec.result else [])[offset: offset + want]
        for i, row in enumerate(rows):
            records.append({
                "id": hashlib.sha256(f"{t.id}:{offset + i}".encode()).hexdigest()[:32],
                "entity": _entity_of(t),
                "fields": dict(zip(rec.result.columns, row)),
                "source": {"connection_id": conn_src.id, "name": conn_src.name, "type": conn_src.type},
                "lineage": {"table": t.qualified_name, "row_offset": offset + i,
                            "query_ref": rec.ref,
                            "fetched_at": datetime.now(timezone.utc).isoformat()},
            })
    return records, tables, warnings, total


@router.get("/records")
def unified_records(
    request: Request,
    entity: str | None = None,
    source: str | None = None,
    table: str | None = None,
    search: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
    format: str = "json",
    dep=Depends(require_api_key),
    db: Session = Depends(get_db),
):
    key, user = dep
    if "read" not in (key.scopes or ["read"]):
        raise error_envelope("forbidden", "This key does not have the 'read' scope", 403)
    if entity and key.entities and entity.lower() not in [e.lower() for e in key.entities]:
        raise error_envelope("forbidden", f"This key is not scoped to the '{entity}' entity", 403)
    limit = max(1, min(limit, 1000))
    offset = _decode_cursor(cursor)
    if search is not None:
        search = search.strip()
        # Allow-list, not escaping. There is no portable literal-escaping rule across the
        # engines this platform supports: MySQL treats backslash as an escape character by
        # default, so doubling quotes alone lets "\' OR 1=1 -- " close the literal. The
        # executor accepts no bind parameters, so the only safe option is to constrain the
        # input. See tests/security/test_injection.py.
        if len(search) > 100 or not re.fullmatch(r"[\w .@/+-]*", search):
            raise error_envelope(
                "bad_search",
                "search may contain only letters, digits, spaces and the characters . @ / + - _ "
                "(max 100 characters)")

    records, tables, warnings, total = _collect(db, key, user, entity=entity, source=source,
                                                table=table, search=search, limit=limit, offset=offset)
    audit(db, user, "unified.records", "api_key", key.id,
          {"entity": entity, "source": source, "returned": len(records)})
    db.commit()

    if format == "ndjson":
        # Streams, so a consumer can pull a large export without either side buffering it.
        def gen() -> Iterator[bytes]:
            for r in records:
                yield (json.dumps(r, default=str) + "\n").encode()
        return StreamingResponse(gen(), media_type="application/x-ndjson")

    # The cursor walks each table forward by the per-table stride, so the next page continues
    # every source in step rather than restarting the first one.
    stride = max(1, limit // max(len(tables), 1))
    next_cursor = _encode_cursor(offset + stride) if records and len(records) >= stride else None
    return envelope({"records": records},
                    page={"limit": limit, "returned": len(records), "next_cursor": next_cursor, "total": total},
                    sources=_source_block(tables), warnings=warnings)


# ---------------------------------------------------------------------------- ask
class AskIn(BaseModel):
    question: str
    conversation_id: str | None = None


@router.post("/ask")
def unified_ask(body: AskIn, dep=Depends(require_api_key), db: Session = Depends(get_db)):
    key, user = dep
    if "ask" not in (key.scopes or []):
        raise error_envelope("forbidden", "This key does not have the 'ask' scope", 403)

    from backend.ai import orchestrator
    from backend.metadata.models import Conversation

    conv = db.get(Conversation, body.conversation_id) if body.conversation_id else None
    if conv is None:
        conv = Conversation(user_id=user.id)
        db.add(conv)
        db.commit()

    answer, payload = "", {}
    for event in orchestrator.run(db, user, conv, body.question):
        if event.get("type") == "done":
            answer, payload = event.get("content", ""), event.get("payload", {})
    db.commit()

    ctx = build_access_context(db, user)
    tables = _visible_tables(db, key, ctx)
    return envelope({
        "answer": answer,
        "conversation_id": conv.id,
        "evidence": payload.get("evidence", []),
        "queries": payload.get("queries", []),
        "confidence": payload.get("confidence"),
        # Part of the contract, not a debug field: a consumer (and an auditor) must be able to
        # see that the model was given tokens rather than real values.
        "anonymization": payload.get("anonymization"),
    }, sources=_source_block(tables), warnings=payload.get("warnings", []))


# ---------------------------------------------------------------------------- key admin
class ApiKeyIn(BaseModel):
    name: str
    scopes: list[str] = Field(default_factory=lambda: ["read"])
    entities: list[str] | None = None
    sources: list[str] | None = None
    rate_limit_per_min: int = 120
    acts_as_user_id: str | None = None


@router.post("/keys", status_code=201)
def create_key(body: ApiKeyIn, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    key = ApiKey(name=body.name, key_hash=hash_key(raw), prefix=raw[:16],
                 user_id=body.acts_as_user_id or user.id, scopes=body.scopes,
                 entities=body.entities, sources=body.sources,
                 rate_limit_per_min=body.rate_limit_per_min)
    db.add(key)
    audit(db, user, "apikey.create", "api_key", key.id, {"name": body.name, "scopes": body.scopes})
    db.commit()
    # Shown exactly once. Only the hash is stored, so it can never be recovered.
    return {"id": key.id, "name": key.name, "key": raw,
            "note": "Copy this now — it is not stored and cannot be shown again."}


@router.get("/keys")
def list_keys(db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    return [{"id": k.id, "name": k.name, "prefix": k.prefix, "scopes": k.scopes,
             "entities": k.entities, "sources": k.sources, "rate_limit_per_min": k.rate_limit_per_min,
             "last_used_at": k.last_used_at, "revoked": k.revoked_at is not None,
             "created_at": k.created_at}
            for k in db.scalars(select(ApiKey).order_by(ApiKey.created_at.desc())).all()]


@router.delete("/keys/{key_id}", status_code=204)
def revoke_key(key_id: str, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    key = db.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(404, "Key not found")
    key.revoked_at = now()
    _RATE.pop(key.id, None)
    audit(db, user, "apikey.revoke", "api_key", key.id, {"name": key.name})
    db.commit()
