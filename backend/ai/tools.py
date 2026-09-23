"""Tools exposed to the LLM. Every tool is permission-aware (goes through AccessContext) and every
data-returning tool records its query reference so answers can cite evidence."""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.ai.sandbox import run_analysis
from backend.data_quality.engine import issues_for_tables
from backend.knowledge_graph.graph import build_graph
from backend.metadata.models import Column, DataSource, Dataset, Entity, GlossaryTerm, Metric, Relationship, Table
from backend.query_engine import metrics as M
from backend.query_engine.executor import execute
from backend.query_engine.periods import Period, comparison_for, default_period, resolve_period, shift
from backend.security.access import AccessContext
from backend.vector_store.store import CatalogVectorStore

log = logging.getLogger(__name__)


def _p(name: str, type_: str, desc: str, required: bool = True, **kw) -> tuple[str, dict, bool]:
    return name, {"type": type_, "description": desc, **kw}, required


def _schema(desc: str, *params) -> dict:
    return {"description": desc, "parameters": {"type": "object", "properties": {n: s for n, s, _ in params}, "required": [n for n, _, r in params if r]}}


TOOL_SPECS: dict[str, dict] = {
    "search_metadata": _schema("Semantic search over the data catalog (tables, columns, metrics, glossary). Use first to find where a concept lives.", _p("query", "string", "Natural-language concept, e.g. 'employee attrition'"), _p("limit", "integer", "max results", False)),
    "search_business_glossary": _schema("Look up official business definitions and rules for a term.", _p("query", "string", "term")),
    "find_relevant_datasets": _schema("Rank tables relevant to a question, with domain, source, freshness and row counts.", _p("question", "string", "the question"), _p("limit", "integer", "max tables", False)),
    "list_metrics": _schema("List all governed business metrics with their formulas. Prefer these over ad-hoc calculations."),
    "get_schema": _schema("Columns of a table with business descriptions, semantic types, sample values and the SQL dialect to use.", _p("table", "string", "table name (optionally schema.table)")),
    "get_column_profile": _schema("Statistical profile of a column (min/max/mean/nulls/distinct/frequency).", _p("table", "string", "table"), _p("column", "string", "column")),
    "get_relationships": _schema("Approved relationships + entity mappings for a table (how it joins to other tables/sources).", _p("table", "string", "table")),
    "get_entity_mapping": _schema("How a canonical entity (Customer, Employee, Product...) is identified in each source system.", _p("entity", "string", "entity name")),
    "execute_sql": _schema("Run a read-only SQL query against ONE data source in its native dialect. Aggregate in SQL; never SELECT * from large tables. Returns rows and a query reference (Q-xxxxx) to cite.", _p("source", "string", "data source name"), _p("sql", "string", "SELECT statement"), _p("purpose", "string", "what this query establishes", False)),
    "calculate_metric": _schema("Compute a governed metric for a period, optionally broken down by a dimension column.", _p("metric", "string", "metric name"), _p("period", "string", "e.g. 'August 2026', '2026-08', 'last quarter'", False), _p("dimension", "string", "column to group by", False)),
    "compare_periods": _schema("Compare a metric between two periods with % change, optionally decomposed by a dimension with contribution analysis.", _p("metric", "string", "metric name"), _p("period", "string", "period, e.g. 'August 2026'"), _p("comparison", "string", "comparison period; default previous period", False), _p("dimension", "string", "column to decompose by", False)),
    "detect_anomalies": _schema("Monthly z-score anomaly detection on a metric's time series.", _p("metric", "string", "metric name"), _p("months", "integer", "lookback months (default 12)", False)),
    "get_data_quality": _schema("Open data-quality issues for a table (or all tables) that may affect conclusions.", _p("table", "string", "table name", False)),
    "execute_python_analysis": _schema("Run sandboxed Python (pandas/numpy/scipy/sklearn) over results of previous queries. DataFrames available as data['Q-000123']. Set `result`.", _p("code", "string", "python code"), _p("query_refs", "array", "query references whose results to load", items={"type": "string"})),
    "generate_chart": _schema("Attach a chart to the answer from a previous query result.", _p("query_ref", "string", "query reference"), _p("chart_type", "string", "bar|line|pie|area", enum=["bar", "line", "pie", "area"]), _p("title", "string", "chart title"), _p("x", "string", "column for x axis / categories"), _p("y", "array", "value column(s)", items={"type": "string"})),
}


def tool_definitions(names: list[str] | None = None) -> list[dict]:
    return [{"name": n, **s} for n, s in TOOL_SPECS.items() if not names or n in names]


class ToolBox:
    def __init__(self, db: Session, ctx: AccessContext, conversation_id: str | None, anchor: date | None = None):
        self.db, self.ctx, self.conversation_id = db, ctx, conversation_id
        self.anchor = anchor or date.today()
        self.queries: list[dict] = []
        self.charts: list[dict] = []
        self.tables_used: set[str] = set()
        self.sources_used: set[str] = set()
        self.calls: list[dict] = []
        self._results: dict[str, dict] = {}

    # ------------------------------------------------------------------ dispatch
    def call(self, name: str, args: dict[str, Any]) -> str:
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return json.dumps({"error": f"unknown tool {name}"})
        try:
            out = fn(**{k: v for k, v in args.items() if not k.startswith("_")})
        except TypeError as e:
            out = {"error": f"bad arguments: {e}"}
        except Exception as e:  # noqa: BLE001
            log.exception("tool %s failed", name)
            out = {"error": f"{type(e).__name__}: {e}"}
        self.calls.append({"tool": name, "args": args, "ok": not (isinstance(out, dict) and out.get("error"))})
        return json.dumps(out, default=str)[:12000]

    # ------------------------------------------------------------------ helpers
    def _visible_tables(self) -> list[Table]:
        ts = self.db.scalars(select(Table).options(selectinload(Table.columns), selectinload(Table.dataset).selectinload(Dataset.source))).all()
        return [t for t in ts if self.ctx.can_see_table(t.id)]

    def _find_table(self, name: str) -> Table | None:
        n = name.lower().strip()
        for t in self._visible_tables():
            if n in {t.table_name.lower(), t.qualified_name.lower(), (t.business_name or "").lower()}:
                return t
        for t in self._visible_tables():
            if n.split(".")[-1] == t.table_name.lower():
                return t
        return None

    def _find_metric(self, name: str) -> Metric | None:
        n = name.lower().strip()
        for m in self.db.scalars(select(Metric).options(selectinload(Metric.table))).all():
            if n in {m.name.lower(), (m.display_name or "").lower()}:
                return m
        for m in self.db.scalars(select(Metric).options(selectinload(Metric.table))).all():
            if n in m.name.lower() or n in (m.display_name or "").lower():
                return m
        return None

    def _record(self, rec) -> dict:
        d = rec.to_dict(preview_rows=40)
        self.queries.append(d)
        if rec.result:
            self._results[rec.ref] = {"columns": rec.result.columns, "rows": rec.result.rows}
        self.tables_used.update(rec.tables)
        self.sources_used.add(rec.source_name)
        return d

    def _period(self, text: str | None) -> Period:
        return (resolve_period(text, self.anchor) if text else None) or default_period(self.anchor)

    # ------------------------------------------------------------------ tools
    def t_search_metadata(self, query: str, limit: int = 12) -> dict:
        hits = CatalogVectorStore(self.db).search(query, None, top_k=limit * 2)
        out = []
        for h in hits:
            if h.object_type == "column":
                c = self.db.get(Column, h.object_id)
                if c and self.ctx.can_see_table(c.table_id) and c.id not in self.ctx.denied_columns:
                    out.append({"type": "column", "table": c.table.qualified_name, "column": c.column_name, "description": c.description or c.business_definition, "semantic_type": c.semantic_type, "score": round(h.score, 3)})
            elif h.object_type == "table":
                t = self.db.get(Table, h.object_id)
                if t and self.ctx.can_see_table(t.id):
                    out.append({"type": "table", "table": t.qualified_name, "source": t.dataset.source.name, "domain": t.business_domain, "description": t.description, "rows": t.row_count, "score": round(h.score, 3)})
            elif h.object_type == "metric":
                m = self.db.get(Metric, h.object_id)
                if m:
                    out.append({"type": "metric", "name": m.name, "description": m.description, "expression": m.expression, "filters": m.filters, "table": m.table.qualified_name if m.table else None, "score": round(h.score, 3)})
            elif h.object_type == "glossary":
                g = self.db.get(GlossaryTerm, h.object_id)
                if g:
                    out.append({"type": "glossary", "term": g.term, "definition": g.definition, "rules": g.rules, "score": round(h.score, 3)})
            if len(out) >= limit:
                break
        return {"results": out}

    def t_search_business_glossary(self, query: str) -> dict:
        hits = CatalogVectorStore(self.db).search(query, None, top_k=5, object_types=["glossary", "metric"])
        out = []
        for h in hits:
            if h.object_type == "glossary":
                g = self.db.get(GlossaryTerm, h.object_id)
                out.append({"term": g.term, "definition": g.definition, "domain": g.domain, "owner": g.owner, "synonyms": g.synonyms, "rules": g.rules, "related": g.related_terms})
            else:
                m = self.db.get(Metric, h.object_id)
                out.append({"metric": m.name, "definition": m.description, "formula": m.expression, "filters": m.filters, "owner": m.owner, "related_metrics": m.related_metrics})
        return {"results": out}

    def t_find_relevant_datasets(self, question: str, limit: int = 8) -> dict:
        from backend.metadata.catalog import source_freshness

        hits = CatalogVectorStore(self.db).search(question, None, top_k=60, object_types=["table", "column"])
        scores: dict[str, float] = {}
        for h in hits:
            tid = h.object_id if h.object_type == "table" else (self.db.get(Column, h.object_id).table_id if h.object_type == "column" else None)
            if tid:
                scores[tid] = scores.get(tid, 0) + h.score
        out = []
        for tid, sc in sorted(scores.items(), key=lambda kv: -kv[1])[:limit]:
            t = self.db.get(Table, tid)
            if t and self.ctx.can_see_table(t.id):
                out.append({"table": t.qualified_name, "source": t.dataset.source.name, "domain": t.business_domain or t.dataset.business_domain, "rows": t.row_count, "date_column": t.date_column, "description": t.description, "freshness": source_freshness(t.dataset.source)["label"], "relevance": round(sc, 3), "metrics": [m.name for m in self.db.scalars(select(Metric).where(Metric.table_id == t.id)).all()]})
        return {"tables": out}

    def t_list_metrics(self) -> dict:
        out = []
        for m in self.db.scalars(select(Metric).options(selectinload(Metric.table))).all():
            row = {"name": m.name, "display_name": m.display_name, "description": m.description, "formula": m.expression, "filters": m.filters, "table": m.table.qualified_name if m.table else None, "source": m.table.dataset.source.name if m.table else None, "domain": m.domain, "unit": m.unit, "dimensions": m.dimensions, "related": m.related_metrics}
            if not m.is_computable:
                row.update(computable=False, native_language=m.native_language, native_formula=m.native_expression,
                           imported_from=m.source_system,
                           note="Definition only — this platform cannot compute a value for it. Never estimate one.")
            if m.source_system:
                row["imported_from"] = m.source_system
            out.append(row)
        return {"metrics": out}

    def t_get_schema(self, table: str) -> dict:
        t = self._find_table(table)
        if not t:
            return {"error": f"table '{table}' not found or not accessible"}
        from backend.connectors.registry import get_connector_class
        from backend.query_engine.dialects import get_dialect

        dialect = get_connector_class(t.dataset.source.type).dialect
        cols = [{"name": c.column_name, "type": c.data_type, "logical_type": c.logical_type, "semantic_type": c.semantic_type, "description": c.business_definition or c.description, "unit": c.unit, "pk": c.is_primary_key, "sensitivity": c.sensitivity, "samples": c.sample_values[:5], "distinct": (c.stats or {}).get("distinct_count"), "masked": self.ctx.should_mask(t, c)} for c in self.ctx.visible_columns(t)]
        return {"table": t.qualified_name, "source": t.dataset.source.name, "dialect": dialect, "dialect_hints": get_dialect(dialect).hints(), "description": t.description, "domain": t.business_domain, "rows": t.row_count, "date_column": t.date_column, "columns": cols}

    def t_get_column_profile(self, table: str, column: str) -> dict:
        t = self._find_table(table)
        if not t:
            return {"error": f"table '{table}' not found"}
        for c in self.ctx.visible_columns(t):
            if c.column_name.lower() == column.lower():
                return {"table": t.qualified_name, "column": c.column_name, "logical_type": c.logical_type, "semantic_type": c.semantic_type, "description": c.description, "business_definition": c.business_definition, "stats": c.stats, "samples": c.sample_values, "sensitivity": c.sensitivity}
        return {"error": f"column '{column}' not found or restricted"}

    def t_get_relationships(self, table: str) -> dict:
        t = self._find_table(table)
        if not t:
            return {"error": f"table '{table}' not found"}
        rels = self.db.scalars(select(Relationship).where(Relationship.status == "approved").options(selectinload(Relationship.from_column).selectinload(Column.table).selectinload(Table.dataset), selectinload(Relationship.to_column).selectinload(Column.table).selectinload(Table.dataset))).all()
        out = []
        for r in rels:
            if t.id in {r.from_column.table_id, r.to_column.table_id}:
                a, b = r.from_column, r.to_column
                other = b if a.table_id == t.id else a
                if self.ctx.can_see_table(other.table_id):
                    out.append({"from": f"{a.table.qualified_name}.{a.column_name}", "to": f"{b.table.qualified_name}.{b.column_name}", "type": r.type, "confidence": r.confidence, "cross_source": r.is_cross_source, "other_source": other.table.dataset.source.name})
        g = build_graph(self.db)
        reach = g.paths_from(t.id, 3)
        multi_hop = [{"table": g.tables[tid].qualified_name, "hops": len(p), "path": " → ".join(e.via for e in p)} for tid, p in reach.items() if self.ctx.can_see_table(tid)]
        return {"table": t.qualified_name, "relationships": out, "reachable": multi_hop[:20]}

    def t_get_entity_mapping(self, entity: str) -> dict:
        ents = self.db.scalars(select(Entity).options(selectinload(Entity.mappings).selectinload(Column.table))).all()
        for e in ents:
            if e.name.lower() == entity.lower().strip():
                return {"entity": e.name, "description": e.description, "mappings": [{"table": m.column.table.qualified_name, "column": m.column.column_name, "source": m.column.table.dataset.source.name, "role": m.role, "confidence": m.confidence} for m in e.mappings if self.ctx.can_see_table(m.column.table_id)]}
        return {"error": f"entity '{entity}' not found", "available": [e.name for e in ents]}

    def t_execute_sql(self, source: str, sql: str, purpose: str | None = None) -> dict:
        src = self._find_source(source)
        if not src:
            return {"error": f"source '{source}' not found", "available": [s.name for s in self.db.scalars(select(DataSource)).all()]}
        rec = execute(self.db, self.ctx, src, sql, purpose=purpose, conversation_id=self.conversation_id, limit=500)
        d = self._record(rec)
        return {"query_ref": rec.ref, "columns": d["columns"], "rows": d["rows"], "row_count": d["row_count"], "truncated": d["truncated"], "error": rec.error}

    def _find_source(self, name: str) -> DataSource | None:
        n = name.lower().strip()
        srcs = self.db.scalars(select(DataSource)).all()
        for s in srcs:
            if s.name.lower() == n:
                return s
        for s in srcs:
            if n in s.name.lower() or s.name.lower() in n:
                return s
        t = self._find_table(name)
        return t.dataset.source if t else None

    def t_calculate_metric(self, metric: str, period: str | None = None, dimension: str | None = None) -> dict:
        m = self._find_metric(metric)
        if not m:
            return {"error": f"metric '{metric}' not defined", "available": [x.name for x in self.db.scalars(select(Metric)).all()]}
        p = self._period(period)
        if dimension:
            vals, rec = M.compute_by_dimension(self.db, self.ctx, m, dimension, p, self.conversation_id)
            self._record(rec)
            return {"metric": m.name, "period": p.label, "by": dimension, "values": vals, "query_ref": rec.ref, "error": rec.error}
        mv = M.compute(self.db, self.ctx, m, p, self.conversation_id)
        self.queries.append({"ref": mv.query_ref, "sql": mv.sql, "source": mv.source, "tables": [mv.table], "columns": ["value"], "rows": [[mv.value]], "error": mv.error})
        self.tables_used.add(mv.table)
        self.sources_used.add(mv.source)
        return {"metric": m.name, "period": p.label, "value": mv.value, "unit": m.unit, "query_ref": mv.query_ref, "source": mv.source, "table": mv.table, "error": mv.error}

    def t_compare_periods(self, metric: str, period: str, comparison: str | None = None, dimension: str | None = None) -> dict:
        m = self._find_metric(metric)
        if not m:
            return {"error": f"metric '{metric}' not defined"}
        p = self._period(period)
        c = resolve_period(comparison, self.anchor) if comparison else comparison_for(period, p)
        if not c:
            c = shift(p, -1)
        a, b = M.compute(self.db, self.ctx, m, c, self.conversation_id), M.compute(self.db, self.ctx, m, p, self.conversation_id)
        for mv in (a, b):
            self.queries.append({"ref": mv.query_ref, "sql": mv.sql, "source": mv.source, "tables": [mv.table], "columns": ["value"], "rows": [[mv.value]], "error": mv.error})
            self.tables_used.add(mv.table)
            self.sources_used.add(mv.source)
        out = {"metric": m.name, "comparison_period": c.label, "period": p.label, "before": a.value, "after": b.value, "change_pct": M.pct_change(a.value, b.value), "query_refs": [a.query_ref, b.query_ref], "source": a.source, "table": a.table}
        if dimension:
            vb, rb = M.compute_by_dimension(self.db, self.ctx, m, dimension, c, self.conversation_id)
            va, ra = M.compute_by_dimension(self.db, self.ctx, m, dimension, p, self.conversation_id)
            self._record(rb)
            self._record(ra)
            total = (b.value or 0) - (a.value or 0)
            segs = []
            for seg in set(vb) | set(va):
                d = va.get(seg, 0) - vb.get(seg, 0)
                segs.append({"segment": seg, "before": vb.get(seg, 0), "after": va.get(seg, 0), "change_pct": M.pct_change(vb.get(seg, 0), va.get(seg, 0)), "contribution_pct": round(100 * d / total, 1) if total else None})
            segs.sort(key=lambda s: -abs(s["contribution_pct"] or 0))
            out["decomposition"] = {"dimension": dimension, "segments": segs[:15], "query_refs": [rb.ref, ra.ref]}
        return out

    def t_detect_anomalies(self, metric: str, months: int = 12) -> dict:
        m = self._find_metric(metric)
        if not m:
            return {"error": f"metric '{metric}' not defined"}
        end = default_period(self.anchor).end
        start = shift(default_period(self.anchor), -(months - 1)).start
        series, rec = M.monthly_series(self.db, self.ctx, m, start, end, self.conversation_id)
        self._record(rec)
        return {"metric": m.name, "series": series, "anomalies": zscore_anomalies(series), "query_ref": rec.ref}

    def t_get_data_quality(self, table: str | None = None) -> dict:
        t = self._find_table(table) if table else None
        if table and not t:
            return {"error": f"table '{table}' not found"}
        return {"issues": issues_for_tables(self.db, [t.id] if t else None)}

    def t_execute_python_analysis(self, code: str, query_refs: list[str]) -> dict:
        data = {r: self._results[r] for r in query_refs if r in self._results}
        missing = [r for r in query_refs if r not in self._results]
        if missing:
            return {"error": f"no results in this session for {missing}; run the query first"}
        return run_analysis(code, data)

    def t_generate_chart(self, query_ref: str, chart_type: str, title: str, x: str, y: list[str]) -> dict:
        res = self._results.get(query_ref)
        if not res:
            return {"error": f"no result for {query_ref}"}
        cols = res["columns"]
        if x not in cols or any(c not in cols for c in y):
            return {"error": f"columns must be among {cols}"}
        xi = cols.index(x)
        chart = {"type": chart_type, "title": title, "x": [r[xi] for r in res["rows"]], "series": [{"name": c, "data": [r[cols.index(c)] for r in res["rows"]]} for c in y], "query_ref": query_ref}
        self.charts.append(chart)
        return {"ok": True, "chart_index": len(self.charts) - 1}


def zscore_anomalies(series: dict[str, float], threshold: float = 2.0) -> list[dict]:
    keys = sorted(series)
    vals = np.array([series[k] for k in keys], dtype=float)
    if len(vals) < 4 or vals.std() == 0:
        return []
    out = []
    for i, k in enumerate(keys):
        rest = np.delete(vals, i)
        if rest.std() == 0:
            continue
        z = (vals[i] - rest.mean()) / rest.std()
        if abs(z) >= threshold:
            out.append({"month": k, "value": float(vals[i]), "z_score": round(float(z), 2), "expected": round(float(rest.mean()), 2)})
    return out
