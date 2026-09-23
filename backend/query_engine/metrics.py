"""Metric computation: generates native SQL for a defined business metric and executes it through
the permission-aware executor. All aggregation is pushed down to the source database."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from backend.metadata.models import DataSource, Metric, Table
from backend.query_engine.dialects import Dialect, get_dialect
from backend.query_engine.executor import ExecutionRecord, execute
from backend.query_engine.periods import Period
from backend.security.access import AccessContext


@dataclass
class MetricValue:
    metric: str
    period: Period
    value: float | None
    query_ref: str
    sql: str
    source: str
    table: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"metric": self.metric, "period": self.period.to_dict(), "value": self.value, "query_ref": self.query_ref, "source": self.source, "table": self.table, "error": self.error}


def _table_source(db: Session, metric: Metric) -> tuple[Table, DataSource, Dialect]:
    t = metric.table or db.get(Table, metric.table_id)
    src = t.dataset.source
    return t, src, get_dialect(_dialect_of(src))


def _dialect_of(src: DataSource) -> str:
    from backend.connectors.registry import get_connector_class

    return get_connector_class(src.type).dialect


def _fill(filters: str | None, period: Period | None) -> str | None:
    """Point-in-time metrics use {period_start}/{period_end} placeholders in their filters."""
    if not filters or not period:
        return filters
    return filters.replace("{period_start}", period.start.isoformat()).replace("{period_end}", period.end.isoformat())


def uses_placeholders(metric: Metric) -> bool:
    return bool(metric.filters and "{period_" in metric.filters)


def metric_sql(metric: Metric, table: Table, d: Dialect, period: Period | None, group_by: str | None = None, extra_filter: str | None = None) -> str:
    where = []
    if metric.filters:
        where.append(f"({_fill(metric.filters, period)})")
    dc = None if uses_placeholders(metric) else (metric.date_column or table.date_column)
    if period and dc:
        where.append(d.date_between(dc, period.start.isoformat(), period.end.isoformat()))
    if extra_filter:
        where.append(f"({extra_filter})")
    w = f" WHERE {' AND '.join(where)}" if where else ""
    tbl = d.table(table.table_name, table.schema_name)
    if group_by:
        return f"SELECT {d.ident(group_by)} AS segment, {metric.expression} AS value FROM {tbl}{w} GROUP BY {d.ident(group_by)} ORDER BY value DESC"
    return f"SELECT {metric.expression} AS value FROM {tbl}{w}"


def monthly_series_sql(metric: Metric, table: Table, d: Dialect, start: date, end: date, extra_filter: str | None = None) -> str:
    dc = metric.date_column or table.date_column
    where = [d.date_between(dc, start.isoformat(), end.isoformat())]
    if metric.filters:
        where.append(f"({metric.filters})")
    if extra_filter:
        where.append(f"({extra_filter})")
    tbl = d.table(table.table_name, table.schema_name)
    mk = d.month_key(dc)
    return f"SELECT {mk} AS month, {metric.expression} AS value FROM {tbl} WHERE {' AND '.join(where)} GROUP BY {mk} ORDER BY {mk}"


def not_computable_reason(metric: Metric) -> str | None:
    """Definition-only metrics (untranslatable imported BI formulas) must never yield a number."""
    if getattr(metric, "is_computable", True):
        return None
    lang = (metric.native_language or "native").upper()
    return (f"'{metric.display_name or metric.name}' is a definition-only metric imported from "
            f"{metric.source_system or 'a BI model'}: its {lang} formula has no exact SQL equivalent here, "
            f"so no value can be computed. Formula: {metric.native_expression or metric.expression}")


def compute(db: Session, ctx: AccessContext, metric: Metric, period: Period | None, conversation_id: str | None = None, extra_filter: str | None = None) -> MetricValue:
    if (reason := not_computable_reason(metric)):
        return MetricValue(metric.name, period, None, "-", "", "", "", reason)
    t, src, d = _table_source(db, metric)
    sql = metric_sql(metric, t, d, period, extra_filter=extra_filter)
    rec = execute(db, ctx, src, sql, purpose=f"metric:{metric.name}", conversation_id=conversation_id)
    val = _num(rec.result.rows[0][0]) if rec.result and rec.result.rows else None
    return MetricValue(metric.name, period, val, rec.ref, rec.sql, src.name, t.qualified_name, rec.error)


def compute_by_dimension(db: Session, ctx: AccessContext, metric: Metric, dimension: str, period: Period, conversation_id: str | None = None) -> tuple[dict[str, float], ExecutionRecord]:
    if (reason := not_computable_reason(metric)):
        return {}, ExecutionRecord("-", "", "", "", [], None, reason, 0)
    t, src, d = _table_source(db, metric)
    sql = metric_sql(metric, t, d, period, group_by=dimension)
    rec = execute(db, ctx, src, sql, purpose=f"metric:{metric.name} by {dimension}", conversation_id=conversation_id)
    out = {}
    if rec.result:
        for seg, v in rec.result.rows:
            out[str(seg)] = _num(v) or 0.0
    return out, rec


def monthly_series(db: Session, ctx: AccessContext, metric: Metric, start: date, end: date, conversation_id: str | None = None, extra_filter: str | None = None) -> tuple[dict[str, float], ExecutionRecord]:
    if (reason := not_computable_reason(metric)):
        raise ValueError(reason)
    t, src, d = _table_source(db, metric)
    if uses_placeholders(metric):  # point-in-time metric: one query per month
        from backend.query_engine.periods import Period as _P, shift as _shift

        series, rec = {}, None
        p = _P(date(start.year, start.month, 1), _shift(_P(date(start.year, start.month, 1), date(start.year, start.month, 1), "", "month"), 1).start, "", "month")
        while p.start < end:
            mv = compute(db, ctx, metric, p, conversation_id, extra_filter)
            series[p.start.strftime("%Y-%m")] = mv.value or 0.0
            rec = ExecutionRecord(mv.query_ref, mv.sql, src.id, src.name, [t.qualified_name], None, mv.error, 0)
            p = _shift(p, 1)
        return series, rec
    if not (metric.date_column or t.date_column):
        raise ValueError(f"metric {metric.name} has no date column")
    sql = monthly_series_sql(metric, t, d, start, end, extra_filter)
    rec = execute(db, ctx, src, sql, purpose=f"trend:{metric.name}", conversation_id=conversation_id)
    series = {}
    if rec.result:
        for m, v in rec.result.rows:
            series[str(m)[:7]] = _num(v) or 0.0
    return series, rec


def dimension_candidates(metric: Metric, table: Table, max_cardinality: int = 60) -> list[str]:
    if metric.dimensions:
        return [c for c in metric.dimensions if any(col.column_name == c for col in table.columns)]
    out = []
    for c in table.columns:
        dist = (c.stats or {}).get("distinct_count") or 0
        if c.semantic_type == "dimension" and 1 < dist <= max_cardinality and c.sensitivity not in {"pii", "restricted"}:
            out.append(c.column_name)
    return out[:6]


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pct_change(before: float | None, after: float | None) -> float | None:
    if before in (None, 0) or after is None:
        return None
    return round(100.0 * (after - before) / abs(before), 2)
