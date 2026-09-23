"""Deterministic cross-domain investigation engine.

Given a primary metric and two periods it:
  1. validates the headline change,
  2. decomposes the change by every candidate dimension (contribution analysis),
  3. walks the knowledge graph to related tables/metrics (multi-hop) and measures their change,
  4. re-segments related metrics by the top driver segment when they share the dimension,
  5. correlates monthly series (labelled as correlation, never causation),
  6. attaches data-quality issues and freshness for every source touched.
Every number carries a query reference so the LLM (or the template renderer) can cite evidence.
Independent per-metric analyses run concurrently."""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.core.db import SessionLocal
from backend.knowledge_graph.graph import KnowledgeGraph, build_graph
from backend.metadata.catalog import source_freshness
from backend.metadata.models import DataQualityIssue, Metric, Table
from backend.query_engine import metrics as M
from backend.query_engine.periods import Period, shift
from backend.security.access import AccessContext

log = logging.getLogger(__name__)


@dataclass
class Evidence:
    id: str
    statement: str
    kind: str  # observed_fact | calculated_metric | correlation | data_quality | freshness
    domain: str | None
    source: str
    table: str
    query_refs: list[str]
    values: dict[str, Any] = field(default_factory=dict)
    strength: str = "strong"  # strong | medium | weak

    def to_dict(self) -> dict:
        return self.__dict__


@dataclass
class Investigation:
    metric: str
    period: Period
    comparison: Period
    headline: dict[str, Any]
    trend: list[dict[str, Any]]
    decompositions: list[dict[str, Any]]
    related: list[dict[str, Any]]
    correlations: list[dict[str, Any]]
    quality_issues: list[dict[str, Any]]
    freshness: list[dict[str, Any]]
    evidence: list[Evidence]
    queries: list[dict[str, Any]]
    sources: list[str]
    tables: list[str]
    reasoning_path: list[str]
    plan: list[str]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric, "period": self.period.to_dict(), "comparison": self.comparison.to_dict(), "headline": self.headline,
            "trend": self.trend, "decompositions": self.decompositions, "related": self.related, "correlations": self.correlations,
            "quality_issues": self.quality_issues, "freshness": self.freshness, "evidence": [e.to_dict() for e in self.evidence],
            "queries": self.queries, "sources": self.sources, "tables": self.tables, "reasoning_path": self.reasoning_path, "plan": self.plan, "warnings": self.warnings,
        }

    def compact(self, max_segments: int = 6) -> dict[str, Any]:
        """Token-efficient view for the LLM."""
        return {
            "metric": self.metric, "period": self.period.label, "comparison": self.comparison.label, "headline": self.headline,
            "trend": self.trend[-12:],
            "decompositions": [{**d, "segments": d["segments"][:max_segments]} for d in self.decompositions],
            "related_metrics": self.related, "correlations": self.correlations, "data_quality_issues": self.quality_issues, "freshness": self.freshness,
            "evidence": [{"id": e.id, "statement": e.statement, "kind": e.kind, "strength": e.strength, "query_refs": e.query_refs, "source": e.source, "table": e.table} for e in self.evidence],
            "warnings": self.warnings,
        }


def investigate(db: Session, ctx: AccessContext, metric: Metric, period: Period, comparison: Period, conversation_id: str | None = None, max_hops: int = 3, graph: KnowledgeGraph | None = None) -> Investigation:
    graph = graph or build_graph(db)
    table = metric.table or db.get(Table, metric.table_id)
    plan = [f"Validate {metric.display_name or metric.name} change: {comparison.label} → {period.label}", "Compute 12-month trend"]
    evidence: list[Evidence] = []
    queries: list[dict] = []
    warnings: list[str] = []
    sources, tables = {table.dataset.source.name}, {table.qualified_name}
    eid = _counter()

    def track(rec) -> None:
        queries.append(rec.to_dict(preview_rows=20))
        if rec.error:
            warnings.append(f"{rec.ref}: {rec.error}")

    # 1. headline
    before = M.compute(db, ctx, metric, comparison, conversation_id)
    after = M.compute(db, ctx, metric, period, conversation_id)
    for mv in (before, after):
        queries.append({"ref": mv.query_ref, "sql": mv.sql, "source": mv.source, "tables": [mv.table], "error": mv.error, "rows": [[mv.value]], "columns": ["value"]})
        if mv.error:
            warnings.append(f"{mv.query_ref}: {mv.error}")
    change = M.pct_change(before.value, after.value)
    headline = {"metric": metric.display_name or metric.name, "before": before.value, "after": after.value, "change_pct": change, "change_abs": (after.value - before.value) if (before.value is not None and after.value is not None) else None, "unit": metric.unit, "format": metric.format, "query_refs": [before.query_ref, after.query_ref], "source": before.source, "table": before.table}
    if change is not None:
        direction = "decreased" if change < 0 else "increased"
        evidence.append(Evidence(next(eid), f"{headline['metric']} {direction} {abs(change):.1f}% from {_fmt(before.value, metric)} in {comparison.label} to {_fmt(after.value, metric)} in {period.label}.", "calculated_metric", metric.domain, before.source, before.table, [before.query_ref, after.query_ref], {"before": before.value, "after": after.value, "change_pct": change}))
    else:
        warnings.append(f"Could not compute {metric.name} for both periods (data may be unavailable).")

    # 2. trend (12 months up to the period)
    trend: list[dict] = []
    trend_series: dict[str, float] = {}
    try:
        start = shift(Period(period.start, period.end, "", "month"), -11).start if period.grain == "month" else date(period.start.year - 1, period.start.month, 1)
        trend_series, rec = M.monthly_series(db, ctx, metric, start, period.end, conversation_id)
        track(rec)
        trend = [{"month": k, "value": v} for k, v in sorted(trend_series.items())]
    except Exception as e:  # noqa: BLE001
        warnings.append(f"trend unavailable: {e}")

    # 3. decomposition by dimensions
    decompositions: list[dict] = []
    additive = bool(re.match(r"^\s*(SUM|COUNT)\s*\(", metric.expression, re.I))  # contribution % only makes sense for additive metrics
    dims = M.dimension_candidates(metric, table)
    plan += [f"Decompose change by {d}" for d in dims]
    drivers: list[tuple[str, str, float]] = []  # (dimension, segment, score) — disproportionate movers
    for dim in dims:
        b, rb = M.compute_by_dimension(db, ctx, metric, dim, comparison, conversation_id)
        a, ra = M.compute_by_dimension(db, ctx, metric, dim, period, conversation_id)
        track(rb)
        track(ra)
        if rb.error or ra.error:
            continue
        total_delta = (after.value or 0) - (before.value or 0)
        segs = []
        for seg in sorted(set(b) | set(a), key=lambda s: (a.get(s, 0) - b.get(s, 0))):
            bv, av = b.get(seg, 0.0), a.get(seg, 0.0)
            delta = av - bv
            segs.append({"segment": seg, "before": bv, "after": av, "change_pct": M.pct_change(bv, av), "change_abs": delta, "contribution_pct": round(100 * delta / total_delta, 1) if (total_delta and additive) else None})
        if not additive:  # rank ratio metrics by how far the segment moved, not by absolute delta
            segs.sort(key=lambda s: (s["change_pct"] or 0) if change < 0 else -(s["change_pct"] or 0))
        if change is not None and change > 0:
            segs.sort(key=lambda s: -s["change_abs"])
        decompositions.append({"dimension": dim, "segments": segs, "query_refs": [rb.ref, ra.ref]})
        if segs and change:
            top = segs[0]
            # a driver is a segment that moved *disproportionately*, not merely a big segment moving with the total
            excess = abs((top["change_pct"] or 0) - change)
            big_enough = (top["contribution_pct"] is not None and abs(top["contribution_pct"]) >= 25) if additive else (top["change_pct"] is not None and excess >= 10)
            if big_enough and excess >= max(5.0, abs(change) / 3):
                contrib = f", contributing {top['contribution_pct']:.0f}% of the total change" if top["contribution_pct"] is not None else ""
                evidence.append(Evidence(next(eid), f"By {dim}: '{top['segment']}' moved {_pct(top['change_pct'])} ({_fmt(top['before'], metric)} → {_fmt(top['after'], metric)}) versus {_pct(change)} overall{contrib}.", "calculated_metric", metric.domain, before.source, before.table, [rb.ref, ra.ref], top, "strong" if (top["contribution_pct"] is not None and abs(top["contribution_pct"]) >= 40) or excess >= 20 else "medium"))
                drivers.append((dim, top["segment"], (abs(top["contribution_pct"]) if top["contribution_pct"] is not None else 50) * excess))
    drivers.sort(key=lambda d: -d[2])
    drivers = drivers[:3]

    # 4. related metrics via knowledge graph (multi-hop)
    related: list[dict] = []
    correlations: list[dict] = []
    reach = graph.paths_from(table.id, max_hops=max_hops)
    candidates: list[tuple[Metric, list]] = []
    all_metrics = db.scalars(select(Metric).where(Metric.id != metric.id).options(selectinload(Metric.table).selectinload(Table.dataset))).all()
    for m in all_metrics:
        if not m.table_id or not m.is_computable or not (m.date_column or (m.table and m.table.date_column) or M.uses_placeholders(m)):
            continue
        if m.table_id == table.id:
            candidates.append((m, []))
        elif m.table_id in reach:
            candidates.append((m, reach[m.table_id]))
    plan += [f"Check related metric {m.display_name or m.name} ({m.domain or 'n/a'})" for m, _ in candidates]
    reasoning_path = [table.qualified_name]

    def analyse_related(item: tuple[Metric, list]) -> dict | None:
        m, path = item
        # each worker gets its own session so analyses run concurrently
        with SessionLocal() as s:
            mm = s.get(Metric, m.id)
            try:
                b = M.compute(s, ctx, mm, comparison, conversation_id)
                a = M.compute(s, ctx, mm, period, conversation_id)
                prior = M.compute(s, ctx, mm, shift(comparison, -1), conversation_id)  # lag detection
                series, srec = M.monthly_series(s, ctx, mm, start, period.end, conversation_id) if trend_series else ({}, None)
                segments = []
                cols = {c.column_name for c in mm.table.columns} if mm.table else set()
                shared = [d for d in drivers if d[0] in cols]
                for dim, seg, _ in shared:
                    sb, rsb = M.compute_by_dimension(s, ctx, mm, dim, comparison, conversation_id)
                    sa, rsa = M.compute_by_dimension(s, ctx, mm, dim, period, conversation_id)
                    segments.append({"dimension": dim, "segment": seg, "before": sb.get(seg), "after": sa.get(seg), "change_pct": M.pct_change(sb.get(seg), sa.get(seg)), "query_refs": [rsb.ref, rsa.ref]})
                if len(shared) >= 2:  # intersection of the two strongest drivers, e.g. region='West' AND product_id='P003'
                    (d1, s1, _), (d2, s2, _) = shared[0], shared[1]
                    flt = f"{d1} = '{s1}' AND {d2} = '{s2}'"
                    cb, ca = M.compute(s, ctx, mm, comparison, conversation_id, extra_filter=flt), M.compute(s, ctx, mm, period, conversation_id, extra_filter=flt)
                    segments.append({"dimension": f"{d1} & {d2}", "segment": f"{s1} & {s2}", "before": cb.value, "after": ca.value, "change_pct": M.pct_change(cb.value, ca.value), "query_refs": [cb.query_ref, ca.query_ref]})
                s.commit()
                return {"metric": mm, "before": b, "after": a, "prior": prior, "series": series, "series_rec": srec, "segments": segments, "path": path}
            except Exception as e:  # noqa: BLE001
                log.warning("related metric %s failed: %s", mm.name, e)
                s.rollback()
                return {"metric": mm, "error": str(e), "path": path}

    db.commit()  # release the write lock before workers open their own sessions
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(analyse_related, candidates))

    primary_vec = np.array([trend_series[k] for k in sorted(trend_series)]) if trend_series else None
    for r in results:
        m = r["metric"]
        hop_desc = " → ".join(f"{e.via}" for e in r["path"]) if r["path"] else "same table"
        if "error" in r:
            warnings.append(f"{m.name}: {r['error']}")
            continue
        b, a = r["before"], r["after"]
        for mv in (b, a):
            queries.append({"ref": mv.query_ref, "sql": mv.sql, "source": mv.source, "tables": [mv.table], "error": mv.error, "rows": [[mv.value]], "columns": ["value"]})
        if r["series_rec"] is not None:
            queries.append(r["series_rec"].to_dict(20))
        ch = M.pct_change(b.value, a.value)
        segs = [sg for sg in r["segments"] if sg["change_pct"] is not None]
        best_seg = max(segs, key=lambda sg: abs(sg["change_pct"]), default=None)
        entry = {"metric": m.display_name or m.name, "name": m.name, "domain": m.domain, "source": b.source, "table": b.table, "before": b.value, "after": a.value, "change_pct": ch, "unit": m.unit, "format": m.format, "direction": m.direction, "path": hop_desc, "hops": len(r["path"]), "query_refs": [b.query_ref, a.query_ref], "segments": segs, "segment": best_seg}
        related.append(entry)
        sources.add(b.source)
        tables.add(b.table)
        if b.table not in reasoning_path:
            reasoning_path.append(b.table)
        lag = M.pct_change(r["prior"].value, b.value)
        lagged = lag is not None and abs(lag) >= 50 and (ch is None or abs(lag) > 2 * abs(ch))
        entry["prior"] = {"period": shift(comparison, -1).label, "value": r["prior"].value, "change_pct_to_comparison": lag, "query_ref": r["prior"].query_ref}
        notable = (ch is not None and abs(ch) >= 5) or (best_seg is not None and abs(best_seg["change_pct"]) >= 10) or lagged
        if notable:
            strength = "medium" if ch is not None and abs(ch) >= 15 else "weak"
            verb = "was unchanged" if not ch else ("fell" if ch < 0 else "rose")
            stmt = f"{entry['metric']} {verb}{'' if not ch else f' {abs(ch):.1f}%'} ({_fmt(b.value, m)} → {_fmt(a.value, m)}) over the same periods [{m.domain or 'n/a'} · {hop_desc}]."
            refs = [b.query_ref, a.query_ref]
            for sg in segs:
                if abs(sg["change_pct"]) >= 10:
                    stmt += f" Within {sg['dimension']}='{sg['segment']}': {_pct(sg['change_pct'])} ({_fmt(sg['before'], m)} → {_fmt(sg['after'], m)})."
                    refs += sg["query_refs"]
            if best_seg and abs(best_seg["change_pct"]) >= abs(ch or 0):
                strength = "strong" if abs(best_seg["change_pct"]) >= 20 else "medium"
            if lagged:
                stmt += f" Note: in the preceding month ({shift(comparison, -1).label} → {comparison.label}) it moved {_pct(lag)} ({_fmt(r['prior'].value, m)} → {_fmt(b.value, m)}) — a lagged effect on {period.label} is possible."
                refs.append(r["prior"].query_ref)
            evidence.append(Evidence(next(eid), stmt, "observed_fact", m.domain, b.source, b.table, refs, {"before": b.value, "after": a.value, "change_pct": ch, "segments": segs}, strength))
        # correlation on monthly series
        if primary_vec is not None and r["series"] and len(r["series"]) >= 6:
            keys = sorted(set(trend_series) & set(r["series"]))
            if len(keys) >= 6:
                x = np.array([trend_series[k] for k in keys])
                y = np.array([r["series"][k] for k in keys])
                if x.std() > 0 and y.std() > 0:
                    rho = float(np.corrcoef(x, y)[0, 1])
                    correlations.append({"metric": entry["metric"], "name": m.name, "pearson_r": round(rho, 3), "months": len(keys), "query_refs": [r["series_rec"].ref] if r["series_rec"] else []})
                    if abs(rho) >= 0.6:
                        evidence.append(Evidence(next(eid), f"Monthly {headline['metric']} and {entry['metric']} are {'positively' if rho > 0 else 'negatively'} correlated (r={rho:.2f}, n={len(keys)} months). Correlation only — not evidence of causation.", "correlation", m.domain, b.source, b.table, [r["series_rec"].ref] if r["series_rec"] else [], {"pearson_r": rho, "n": len(keys)}, "medium" if abs(rho) >= 0.8 else "weak"))

    related.sort(key=lambda e: -(abs(e["change_pct"]) if e["change_pct"] is not None else 0))

    # 5. data quality + freshness
    table_ids = {table.id} | {m.table_id for m, _ in candidates}
    issues = db.scalars(select(DataQualityIssue).where(DataQualityIssue.table_id.in_(table_ids), DataQualityIssue.status == "open").options(selectinload(DataQualityIssue.table))).all()
    quality_issues = [{"table": i.table.qualified_name if i.table else None, "rule": i.rule, "severity": i.severity, "message": i.message} for i in issues]
    for i in issues[:5]:
        evidence.append(Evidence(next(eid), f"Data quality {i.severity}: {i.message}", "data_quality", None, i.table.dataset.source.name if i.table else "", i.table.qualified_name if i.table else "", [], {"rule": i.rule}, "medium"))
    seen_src = {}
    for t in [table] + [m.table for m, _ in candidates if m.table]:
        src = t.dataset.source
        if src.id not in seen_src:
            seen_src[src.id] = {"source": src.name, "type": src.type, **source_freshness(src)}
    freshness = list(seen_src.values())
    stale = [f["source"] for f in freshness if f["age_minutes"] is not None and f["age_minutes"] > 60 * 24 * 3]
    if stale:
        warnings.append(f"Stale data (older than 3 days): {', '.join(stale)}")
    plan += ["Cross-reference findings", "Produce evidence-backed explanation"]
    return Investigation(metric.name, period, comparison, headline, trend, decompositions, related, correlations, quality_issues, freshness, evidence, queries, sorted(sources), sorted(tables), reasoning_path, plan, warnings)


def _counter():
    i = 0
    while True:
        i += 1
        yield f"E{i}"


def _fmt(v: float | None, m: Metric) -> str:
    if v is None:
        return "n/a"
    if m.format == "percent":
        return f"{v:.1f}%"
    if m.format == "currency":
        return f"{m.unit or ''} {v:,.0f}".strip()
    return f"{v:,.2f}".rstrip("0").rstrip(".") if abs(v) < 1000 else f"{v:,.0f}"


def _pct(p: float | None) -> str:
    return "n/a" if p is None else f"{p:+.1f}%"
