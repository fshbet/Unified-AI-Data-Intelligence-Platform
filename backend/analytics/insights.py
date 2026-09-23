"""Proactive insight engine: scans every governed metric's monthly series for anomalies and
month-over-month shocks, then runs a shallow investigation for context. Also surfaces critical
data-quality issues as insights."""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.ai.tools import zscore_anomalies
from backend.analytics.investigation import investigate
from backend.knowledge_graph.graph import build_graph
from backend.metadata.models import DataQualityIssue, Insight, Metric, Table, User
from backend.query_engine import metrics as M
from backend.query_engine.periods import Period, default_period, shift
from backend.security.access import build_access_context

log = logging.getLogger(__name__)


def data_anchor(db: Session) -> date:
    """Latest date across all profiled tables = 'today' for analytics."""
    latest = None
    for t in db.scalars(select(Table)).all():
        for c in t.columns:
            if c.column_name == t.date_column and (c.stats or {}).get("max"):
                try:
                    d = date.fromisoformat(c.stats["max"][:10])
                    latest = d if latest is None or d > latest else latest
                except ValueError:
                    pass
    return latest or date.today()


def generate_insights(db: Session, admin: User, months: int = 12, shock_pct: float = 10.0) -> list[Insight]:
    ctx = build_access_context(db, admin)
    anchor = data_anchor(db)
    graph = build_graph(db)
    created: list[Insight] = []
    existing = {(i.metric_id, i.period, i.kind) for i in db.scalars(select(Insight)).all()}
    for m in db.scalars(select(Metric).options(selectinload(Metric.table))).all():
        if not m.table_id or not m.is_computable or not (m.date_column or (m.table and m.table.date_column) or M.uses_placeholders(m)):
            continue
        cur = default_period(anchor)
        start = shift(cur, -(months - 1)).start
        try:
            series, _ = M.monthly_series(db, ctx, m, start, cur.end)
        except Exception as e:  # noqa: BLE001
            log.warning("insight series failed for %s: %s", m.name, e)
            continue
        keys = sorted(series)
        if len(keys) < 3:
            continue
        anomalies = {a["month"]: a for a in zscore_anomalies(series)}
        for idx in range(len(keys) - 1, max(len(keys) - 4, 0), -1):  # the last three months
            last, prev = keys[idx], keys[idx - 1]
            change = M.pct_change(series[prev], series[last])
            is_shock = change is not None and abs(change) >= shock_pct
            if change is None or abs(change) < 2 or not (is_shock or last in anomalies) or (m.id, last, "anomaly") in existing:
                continue
            created.extend(_insight_for(db, ctx, graph, m, last, prev, series, change, anomalies))
            existing.add((m.id, last, "anomaly"))
    _quality_insights(db, existing, created)
    db.flush()
    return created


def _insight_for(db, ctx, graph, m, last, prev, series, change, anomalies):
    created = []
    if True:
        bad = (change or 0) < 0 if m.direction == "up" else (change or 0) > 0
        severity = "critical" if bad and abs(change or 0) >= 25 else ("warning" if bad else "info")
        p = Period(date.fromisoformat(last + "-01"), shift(Period(date.fromisoformat(last + "-01"), date.fromisoformat(last + "-01"), "", "month"), 1).start, date.fromisoformat(last + "-01").strftime("%B %Y"), "month")
        try:
            inv = investigate(db, ctx, m, p, shift(p, -1), graph=graph, max_hops=2)
            factors = [{"statement": e.statement, "strength": e.strength, "domain": e.domain, "query_refs": e.query_refs} for e in inv.evidence if e.kind != "calculated_metric" or e.id != "E1"][:6]
            details = {"change_pct": change, "before": series[prev], "after": series[last], "z_score": anomalies.get(last, {}).get("z_score"), "factors": factors, "top_segments": [{"dimension": d["dimension"], "segment": d["segments"][0]["segment"], "change_pct": d["segments"][0]["change_pct"], "contribution_pct": d["segments"][0]["contribution_pct"]} for d in inv.decompositions if d["segments"]], "related": inv.related[:8], "sources": inv.sources}
        except Exception as e:  # noqa: BLE001
            log.warning("insight investigation failed for %s: %s", m.name, e)
            details = {"change_pct": change, "before": series[prev], "after": series[last], "factors": []}
        title = f"{m.display_name or m.name} {'declined' if (change or 0) < 0 else 'increased'} {abs(change or 0):.1f}% in {p.label}"
        summary = f"{m.display_name or m.name} moved from {series[prev]:,.0f} to {series[last]:,.0f} ({change:+.1f}%) versus {shift(p, -1).label}." + (f" {len(details.get('factors', []))} related signals detected." if details.get("factors") else "")
        ins = Insight(title=title, summary=summary, severity=severity, kind="anomaly", metric_id=m.id, period=last, details=details)
        db.add(ins)
        created.append(ins)
    return created


def _quality_insights(db, existing, created):
    # critical data quality → insight
    for i in db.scalars(select(DataQualityIssue).where(DataQualityIssue.status == "open", DataQualityIssue.severity == "critical").options(selectinload(DataQualityIssue.table))).all():
        if (i.id, None, "quality") in existing:
            continue
        ins = Insight(title=f"Data quality: {i.rule.replace('_', ' ')} in {i.table.qualified_name if i.table else 'unknown'}", summary=i.message, severity="warning", kind="quality", metric_id=i.id, period=None, details={"issue_id": i.id})
        db.add(ins)
        created.append(ins)
