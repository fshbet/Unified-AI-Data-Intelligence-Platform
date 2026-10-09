"""Permission-aware, audited SQL execution against a registered data source."""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime
from decimal import Decimal
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from backend.connectors.base import QueryResult
from backend.core.config import settings
from backend.metadata.catalog import connector_for
from backend.metadata.models import DataSource, Dataset, QueryLog, Table
from backend.query_engine.sql_safety import UnsafeSQL, ensure_limit, referenced_tables, validate_read_only
from backend.security.access import AccessContext, mask_result

log = logging.getLogger(__name__)


class AccessDenied(PermissionError):
    pass


@dataclass
class ExecutionRecord:
    ref: str
    sql: str
    source_id: str
    source_name: str
    tables: list[str]
    result: QueryResult | None
    error: str | None
    duration_ms: int

    def to_dict(self, preview_rows: int = 50) -> dict[str, Any]:
        return {
            "ref": self.ref, "sql": self.sql, "source_id": self.source_id, "source": self.source_name, "tables": self.tables,
            "columns": self.result.columns if self.result else [], "rows": self.result.rows[:preview_rows] if self.result else [],
            "row_count": len(self.result.rows) if self.result else 0, "truncated": bool(self.result and self.result.truncated),
            "error": self.error, "duration_ms": self.duration_ms,
        }


def _jsonable(value: Any) -> Any:
    """Coerce a cell into something the JSON column can hold, preserving readability."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    item = getattr(value, "item", None)  # numpy / pandas scalars
    if callable(item):
        try:
            return _jsonable(item())
        except (ValueError, TypeError):
            pass
    isoformat = getattr(value, "isoformat", None)  # pandas Timestamp, Timedelta
    if callable(isoformat):
        try:
            return isoformat()
        except (ValueError, TypeError):
            pass
    return str(value)


def _next_ref(db: Session) -> str:
    # ponytail: random 8-hex ref; collision odds negligible and safe under concurrent sessions
    return "Q-" + uuid.uuid4().hex[:8].upper()


def _source_tables(db: Session, source_id: str) -> list[Table]:
    return db.scalars(select(Table).join(Dataset).where(Dataset.source_id == source_id).options(selectinload(Table.columns))).all()


def _table_regex(t: Table) -> re.Pattern:
    q = r'["`\[]?'
    name = re.escape(t.table_name)
    schema = rf"(?:{q}{re.escape(t.schema_name)}{q}\.)?" if t.schema_name else ""
    return re.compile(rf"(\b(?:FROM|JOIN)\s+){schema}{q}{name}{q}(?=[\s,;)]|$)", re.I)


def execute(db: Session, ctx: AccessContext, source: DataSource, sql: str, purpose: str | None = None,
            conversation_id: str | None = None, limit: int | None = None) -> ExecutionRecord:
    ref = _next_ref(db)
    t0 = time.perf_counter()
    tables = _source_tables(db, source.id)
    by_name = {t.table_name.lower(): t for t in tables}
    by_name.update({t.qualified_name.lower(): t for t in tables})
    log_row = QueryLog(ref=ref, user_id=ctx.user.id, source_id=source.id, conversation_id=conversation_id, sql=sql, purpose=purpose, status="ok")
    used: list[Table] = []
    try:
        clean = validate_read_only(sql)
        if not source.is_enabled:
            raise AccessDenied(f"Data source '{source.name}' is disabled")
        for name in referenced_tables(clean):
            t = by_name.get(name) or by_name.get(name.split(".")[-1])
            if t is None:
                continue  # unknown identifier: let the database reject it
            if not ctx.can_see_table(t.id):
                raise AccessDenied(f"You do not have access to table '{t.qualified_name}'")
            for c in t.columns:
                if c.id in ctx.denied_columns and re.search(rf"\b{re.escape(c.column_name)}\b", clean, re.I):
                    raise AccessDenied(f"Column '{t.qualified_name}.{c.column_name}' is restricted for your role")
            used.append(t)
        # row-level security: substitute filtered subqueries
        # ponytail: regex table substitution; swap for sqlglot AST rewrite if queries get exotic
        conn = connector_for(source, db)
        for t in used:
            pred = ctx.row_filters.get(t.id)
            if pred:
                clean = _table_regex(t).sub(lambda m: f"{m.group(1)}(SELECT * FROM {conn.quote_table(t.table_name, t.schema_name)} WHERE {pred}) AS {conn.quote_ident(t.table_name)}", clean)
        final = ensure_limit(clean, limit or settings.max_result_rows, conn.dialect)
        try:
            result = conn.execute_query(final, limit=limit or settings.max_result_rows, timeout=settings.query_timeout_seconds)
        finally:
            conn.close()
        rows = result.rows
        for t in used:
            rows = mask_result(result.columns, rows, t, ctx)
        result.rows = rows
        dur = int((time.perf_counter() - t0) * 1000)
        log_row.row_count, log_row.duration_ms, log_row.sql = len(rows), dur, final
        # JSON-safe: pandas/DuckDB hand back Timestamp, Decimal, date and numpy scalars, none of
        # which the JSON column can store. Any query selecting a date column used to fail here on
        # flush, taking the whole request down with a PendingRollbackError.
        log_row.result_preview = [result.columns] + [[_jsonable(v) for v in r] for r in rows[:20]]
        db.add(log_row)
        db.flush()
        return ExecutionRecord(ref, final, source.id, source.name, [t.qualified_name for t in used], result, None, dur)
    except (UnsafeSQL, AccessDenied) as e:
        log_row.status, log_row.error = "blocked", str(e)
        db.add(log_row)
        db.flush()
        return ExecutionRecord(ref, sql, source.id, source.name, [t.qualified_name for t in used], None, str(e), int((time.perf_counter() - t0) * 1000))
    except Exception as e:  # noqa: BLE001
        msg = str(e).split("\n")[0][:500]
        log_row.status, log_row.error = "error", msg
        db.add(log_row)
        db.flush()
        log.info("query error %s: %s", ref, msg)
        return ExecutionRecord(ref, sql, source.id, source.name, [t.qualified_name for t in used], None, msg, int((time.perf_counter() - t0) * 1000))
