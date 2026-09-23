"""End-to-end on the synthetic dataset: connectors (CSV/JSON/SQLite), catalog sync + profiling, PII,
relationship discovery, metric computation, permissions/RLS/masking, investigation engine, planner,
data quality, insights, and the orchestrator without an LLM."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.analytics.insights import data_anchor
from backend.analytics.investigation import investigate
from backend.metadata.models import Column, DataSource, Metric, Permission, Relationship, Table
from backend.query_engine import metrics as M
from backend.query_engine.executor import execute
from backend.query_engine.periods import resolve_period, shift
from backend.security.access import build_access_context

pytestmark = pytest.mark.usefixtures("demo_sources", "demo_metrics")


def _src(db, name):
    return db.scalar(select(DataSource).where(DataSource.name == name))


def _table(db, name):
    return db.scalar(select(Table).where(Table.table_name == name))


def test_catalog_discovered_and_profiled(db):
    sales = _table(db, "sales")
    assert sales.row_count > 30000 and sales.date_column == "order_date"
    cols = {c.column_name: c for c in sales.columns}
    assert cols["revenue"].semantic_type == "measure" and cols["region"].semantic_type == "dimension"
    assert cols["order_id"].stats["is_unique"]
    hr = {c.column_name: c for c in _table(db, "hr").columns}
    assert hr["email"].pii_type == "email" and hr["salary"].sensitivity == "sensitive"
    support = _table(db, "support")  # JSON wrapped in {"data": [...]} got unnested
    assert "ticket_date" in {c.column_name for c in support.columns}
    assert _table(db, "accounts") is not None  # SQLite connector


def test_relationship_discovery_suggested_cross_source(db):
    rels = db.scalars(select(Relationship)).all()
    pairs = {(r.from_column.table.table_name, r.from_column.column_name, r.to_column.table.table_name, r.to_column.column_name) for r in rels}
    # customer_id ↔ customer_code across Sales and CRM(SQLite) must have been suggested by value overlap + name alias
    assert any({a[0], a[2]} == {"sales", "accounts"} and "customer" in a[1] for a in pairs)
    assert any(r.status == "suggested" and r.confidence >= 0.5 for r in rels)


def test_metric_computation_and_decomposition(db, admin):
    ctx = build_access_context(db, admin)
    rev = db.scalar(select(Metric).where(Metric.name == "revenue"))
    aug, jul = resolve_period("August 2026", data_anchor(db)), resolve_period("July 2026", data_anchor(db))
    a, b = M.compute(db, ctx, rev, aug), M.compute(db, ctx, rev, jul)
    assert a.value and b.value and a.value < b.value
    assert M.pct_change(b.value, a.value) < -10
    by_region, rec = M.compute_by_dimension(db, ctx, rev, "region", aug)
    assert set(by_region) == {"North", "South", "East", "West"} and rec.error is None
    hc = db.scalar(select(Metric).where(Metric.name == "sales_headcount"))
    assert "{period_start}" in hc.filters
    west_jul, _ = M.compute_by_dimension(db, ctx, hc, "region", jul)
    west_aug, _ = M.compute_by_dimension(db, ctx, hc, "region", aug)
    assert west_aug["West"] < west_jul["West"]


def test_investigation_finds_cross_domain_drivers(db, admin):
    ctx = build_access_context(db, admin)
    rev = db.scalar(select(Metric).where(Metric.name == "revenue"))
    aug = resolve_period("August 2026", data_anchor(db))
    inv = investigate(db, ctx, rev, aug, shift(aug, -1))
    assert inv.headline["change_pct"] < -10
    region = next(d for d in inv.decompositions if d["dimension"] == "region")
    assert region["segments"][0]["segment"] == "West"
    domains = {r["domain"] for r in inv.related}
    assert {"hr", "inventory", "support"} <= domains, "multi-hop traversal must reach HR, Inventory and Support"
    statements = " ".join(e.statement for e in inv.evidence)
    assert "West" in statements and "Sales Headcount" in statements and "Product Availability" in statements
    assert all(q["ref"].startswith("Q-") for q in inv.queries)
    assert any(e.kind == "correlation" and "not evidence of causation" in e.statement for e in inv.evidence)


def test_permissions_rls_and_masking(db, admin, viewer):
    hr = _table(db, "hr")
    email = next(c for c in hr.columns if c.column_name == "email")
    salary = next(c for c in hr.columns if c.column_name == "salary")
    db.add(Permission(role="viewer", resource_type="column", resource_id=salary.id, access="deny"))
    db.add(Permission(role="viewer", resource_type="table", resource_id=hr.id, access="allow", row_filter="department = 'Sales'"))
    db.commit()
    vctx = build_access_context(db, viewer)
    src = _src(db, "HR")
    # column denied
    rec = execute(db, vctx, src, "SELECT salary FROM hr")
    assert rec.error and "restricted" in rec.error
    # row-level security applied and PII masked
    rec = execute(db, vctx, src, "SELECT department, email FROM hr", limit=1000)
    assert rec.error is None
    assert {r[0] for r in rec.result.rows} == {"Sales"}
    assert all("***" in r[1] for r in rec.result.rows)
    # a table not allowed (default-deny once an allow rule exists)
    rec = execute(db, vctx, _src(db, "Sales"), "SELECT * FROM sales")
    assert rec.error and "access" in rec.error.lower()
    # admin unaffected
    rec = execute(db, build_access_context(db, admin), src, "SELECT email FROM hr", limit=5)
    assert "@" in rec.result.rows[0][0] and "***" not in rec.result.rows[0][0]
    # write attempts blocked regardless of role
    rec = execute(db, build_access_context(db, admin), src, "DELETE FROM hr")
    assert rec.error and "blocked" not in rec.error.lower() or "SELECT" in rec.error
    db.query(Permission).delete()
    db.commit()


def test_planner_follow_ups(db):
    from backend.ai.planner import plan_question

    anchor = data_anchor(db)
    p1 = plan_question(db, "Why did revenue decline in August?", anchor)
    assert p1.intent == "diagnostic" and p1.metric == "revenue" and p1.period["label"] == "August 2026"
    ctx = {"metric": p1.metric, "metric_id": p1.metric_id, "period": p1.period, "comparison": p1.comparison, "intent": p1.intent}
    p2 = plan_question(db, "Was it because of employee attrition?", anchor, ctx)
    assert p2.is_follow_up and p2.metric == "revenue" and p2.period["label"] == "August 2026" and "metric" in p2.inherited
    p3 = plan_question(db, "Which region caused the decline?", anchor, ctx)
    assert p3.metric == "revenue" and "region" in p3.dimensions
    p4 = plan_question(db, "How does it compare with last year?", anchor, ctx)
    assert p4.comparison["label"] == "August 2025"


def test_data_quality_and_insights(db, admin):
    from backend.analytics.insights import generate_insights
    from backend.data_quality.engine import run_quality_checks

    issues = run_quality_checks(db, _table(db, "hr"))
    assert any(i.rule == "missing_values" and i.severity == "info" for i in issues)  # exit_date is optional
    created = generate_insights(db, admin, months=8)
    db.commit()
    assert any("Revenue" in i.title or "Support Tickets" in i.title for i in created)


def test_orchestrator_without_llm_renders_deterministic_answer(db, admin):
    from backend.ai import orchestrator
    from backend.metadata.models import AIProviderConfig, Conversation

    db.query(AIProviderConfig).delete()
    conv = Conversation(user_id=admin.id)
    db.add(conv)
    db.commit()
    events = list(orchestrator.run(db, admin, conv, "Why did revenue decline in August?"))
    types = [e["type"] for e in events]
    assert "plan" in types and "investigation" in types and types[-1] == "done"
    done = events[-1]
    assert "West" in done["content"] and "[Q-" in done["content"]
    assert done["payload"]["confidence"]["level"] in {"high", "medium"}
    assert {"Sales", "HR", "Inventory", "Support"} <= set(done["payload"]["sources"])
    assert len(done["payload"]["charts"]) >= 2
    assert conv.context["metric"] == "revenue"
    # follow-up keeps context
    events = list(orchestrator.run(db, admin, conv, "Was it because of employee attrition?"))
    assert events[-1]["payload"]["plan"]["is_follow_up"] and events[-1]["payload"]["plan"]["metric"] == "revenue"


def test_python_sandbox_is_restricted():
    from backend.ai.sandbox import run_analysis

    data = {"Q-1": {"columns": ["x", "y"], "rows": [[1, 2], [2, 4], [3, 6]]}}
    ok = run_analysis("df = data['Q-1']\nresult = {'corr': float(df.x.corr(df.y))}", data)
    assert ok["ok"] and abs(ok["result"]["corr"] - 1) < 1e-9
    bad = run_analysis("import os\nresult = os.listdir('.')", data)
    assert not bad["ok"] and "not allowed" in bad["error"]
    bad = run_analysis("result = open('x.txt','w')", data)
    assert not bad["ok"]
    slow = run_analysis("while True: pass", data, timeout=3)
    assert not slow["ok"] and "timeout" in slow["error"]
