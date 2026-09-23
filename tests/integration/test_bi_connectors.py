"""Power BI and Tableau connectors + semantic-model import.

Both platforms are exercised against the stub servers in `scripts/bi_demo_stubs.py`, which speak the
real wire protocols (Entra ID token + `executeQueries` for Power BI; REST sign-in + Metadata GraphQL
+ VizQL Data Service for Tableau). The connector code under test is exactly the code that runs in
production, and the shared stubs mean the demo script cannot drift away from what is tested.
"""
from __future__ import annotations

import threading
from http.server import HTTPServer

import pytest
from sqlalchemy import select

from backend.connectors.registry import create_connector
from backend.metadata.models import DataSource, Metric, Relationship, Table
from scripts.bi_demo_stubs import PBIHandler, TableauHandler

def _serve(handler) -> tuple[HTTPServer, str]:
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


@pytest.fixture(scope="module")
def pbi():
    srv, base = _serve(PBIHandler)
    yield base
    srv.shutdown()


@pytest.fixture(scope="module")
def tableau():
    srv, base = _serve(TableauHandler)
    yield base
    srv.shutdown()


def pbi_config(base: str) -> dict:
    return {"dataset_id": "ds-1", "auth_mode": "service_principal", "tenant_id": "t1",
            "client_id": "c1", "client_secret": "shh", "api_base": base, "authority": base, "max_rows": 1000}


def tableau_config(base: str) -> dict:
    return {"server_url": base, "auth_mode": "pat", "pat_name": "edi", "pat_secret": "secret",
            "datasource_luid": "ds-luid-1", "api_version": "3.22", "max_rows": 1000}


# ---------------------------------------------------------------- Power BI
def test_powerbi_reads_semantic_model_and_rows(pbi):
    conn = create_connector("powerbi", "t_pbi", pbi_config(pbi))
    res = conn.test_connection()
    assert res["ok"], res
    assert res["model"]["measures"] == 6 and res["model"]["relationships"] == 1

    model = conn.fetch_semantic_model()
    assert {t.name for t in model.tables} == {"Sales", "Product"}  # hidden table excluded
    sales = next(t for t in model.tables if t.name == "Sales")
    assert "RowNumber-2662979B" not in {c.name for c in sales.columns}  # internal column dropped
    assert next(c for c in sales.columns if c.name == "Revenue").description == "Net revenue for the line"
    rel = model.relationships[0]
    assert (rel.from_table, rel.from_column, rel.to_table, rel.to_column) == ("Sales", "ProductKey", "Product", "ProductKey")
    assert rel.cardinality == "many_to_one"

    conn.refresh()
    assert {t.name for t in conn.list_tables()} == {"Sales", "Product"}
    r = conn.execute_query('SELECT SUM("Revenue") AS s FROM "Sales"')
    assert r.rows[0][0] == pytest.approx(400.0)
    assert conn.list_databases() == ["Finance Workspace"]


def test_powerbi_access_token_mode_and_auth_failure(pbi):
    ok = create_connector("powerbi", "t_pbi2", {**pbi_config(pbi), "auth_mode": "access_token", "access_token": "stub-token"})
    assert ok.test_connection()["ok"]
    bad = create_connector("powerbi", "t_pbi3", {**pbi_config(pbi), "auth_mode": "access_token", "access_token": "wrong"})
    res = bad.test_connection()
    assert not res["ok"] and "401" in res["message"]


# ---------------------------------------------------------------- Tableau
def test_tableau_reads_datasource_and_rows(tableau):
    conn = create_connector("tableau", "t_tab", tableau_config(tableau))
    res = conn.test_connection()
    assert res["ok"], res

    model = conn.fetch_semantic_model()
    assert model.name == "Superstore Sales" and model.language == "tableau"
    table = model.tables[0]
    assert table.name == "superstore_sales"
    # aggregating calculations become measures; row-level ones stay columns
    assert {m.name for m in model.measures} == {"Profit Ratio", "Running Sales"}
    assert "Unit Margin" in {c.name for c in table.columns}
    assert next(c for c in table.columns if c.name == "Region").description == "Sales territory"

    conn.refresh()
    r = conn.execute_query('SELECT SUM("Sales Amount") AS s FROM "superstore_sales"')
    assert r.rows[0][0] == pytest.approx(400.0)


def test_tableau_metadata_only_mode(tableau):
    conn = create_connector("tableau", "t_tab_meta", {**tableau_config(tableau), "metadata_only": True})
    conn.refresh()
    t = conn.get_schema("superstore_sales")
    assert t.row_count == 0 and "Region" in {c.name for c in t.columns}


# ---------------------------------------------------------------- import into the catalog
@pytest.mark.parametrize("kind", ["powerbi", "tableau"])
def test_semantic_model_import(db, pbi, tableau, kind):
    from backend.workers.jobs import full_sync

    cfg = pbi_config(pbi) if kind == "powerbi" else tableau_config(tableau)
    name = {"powerbi": "PBI Sales Model", "tableau": "Tableau Superstore"}[kind]
    src = db.scalar(select(DataSource).where(DataSource.name == name))
    if src is None:
        src = DataSource(name=name, type=kind, config=cfg, department="Finance", owner="bi@example.com")
        db.add(src)
        db.commit()
    out = full_sync(db, src.id)
    db.commit()

    assert src.status in ("connected", "imported"), src.last_error
    imported = out["semantic_model"]
    assert imported and not imported.get("error"), imported

    if kind == "powerbi":
        # computable measures translated to SQL
        total = db.scalar(select(Metric).where(Metric.name == "total_revenue"))
        assert total.is_computable and total.expression == 'SUM("Revenue")'
        assert total.native_language == "dax" and total.native_expression == "SUM('Sales'[Revenue])"
        assert total.source_system.startswith("Power BI")
        assert total.unit == "INR" and total.format == "currency"

        posted = db.scalar(select(Metric).where(Metric.name == "posted_revenue"))
        assert posted.is_computable and posted.filters == "\"Status\" = 'POSTED'"

        assert db.scalar(select(Metric).where(Metric.name == "order_count")).expression == "COUNT(*)"

        # time intelligence has no exact SQL equivalent -> definition only, formula preserved
        ytd = db.scalar(select(Metric).where(Metric.name == "revenue_ytd"))
        assert ytd is not None and ytd.is_computable is False
        assert ytd.native_expression.startswith("TOTALYTD")
        assert "TOTALYTD" in ytd.description and "definition only" in ytd.description.lower()

        # hidden measures are not imported
        assert db.scalar(select(Metric).where(Metric.name == "hidden_helper")) is None

        # the declared relationship arrives pre-approved
        sales = db.scalar(select(Table).where(Table.table_name == "Sales"))
        col_ids = [c.id for c in sales.columns]
        rel = db.scalar(select(Relationship).where(Relationship.from_column_id.in_(col_ids),
                                                   Relationship.created_by == f"import:{src.name}"))
        assert rel is not None and rel.status == "approved" and rel.evidence["signal"] == "bi_semantic_model"

        # field descriptions land on the catalog columns
        revenue = next(c for c in sales.columns if c.column_name == "Revenue")
        assert revenue.business_definition == "Net revenue for the line"
    else:
        ratio = db.scalar(select(Metric).where(Metric.name == "profit_ratio"))
        assert ratio.is_computable and "SUM(\"Profit\")" in ratio.expression
        assert ratio.native_language == "tableau"
        running = db.scalar(select(Metric).where(Metric.name == "running_sales"))
        assert running is not None and running.is_computable is False
        assert "WINDOW_SUM" in (running.native_expression or "")


def test_definition_only_metric_never_produces_a_number(db, admin, pbi):
    """The whole point of is_computable: the engine must refuse, not estimate."""
    from backend.analytics.insights import data_anchor
    from backend.query_engine import metrics as M
    from backend.query_engine.periods import resolve_period
    from backend.security.access import build_access_context

    ytd = db.scalar(select(Metric).where(Metric.name == "revenue_ytd"))
    if ytd is None:
        pytest.skip("power bi import test did not run")
    ctx = build_access_context(db, admin)
    period = resolve_period("August 2026", data_anchor(db))

    mv = M.compute(db, ctx, ytd, period)
    assert mv.value is None and "definition-only" in (mv.error or "")

    vals, rec = M.compute_by_dimension(db, ctx, ytd, "Status", period)
    assert vals == {} and "definition-only" in (rec.error or "")

    with pytest.raises(ValueError, match="definition-only"):
        M.monthly_series(db, ctx, ytd, period.start, period.end)

    # and it is excluded from automated investigation candidates
    assert ytd.is_computable is False


def test_assistant_explains_a_definition_only_metric_without_estimating(db, admin, pbi):
    """Asking about an untranslatable measure must yield its definition, never a number."""
    from backend.ai import orchestrator
    from backend.metadata.models import AIProviderConfig, Conversation

    ytd = db.scalar(select(Metric).where(Metric.name == "revenue_ytd"))
    if ytd is None:
        pytest.skip("power bi import test did not run")
    db.query(AIProviderConfig).delete()
    conv = Conversation(user_id=admin.id)
    db.add(conv)
    db.commit()

    done = list(orchestrator.run(db, admin, conv, "What is Revenue YTD?"))[-1]
    text = done["content"]
    assert done["payload"]["investigation"] is None, "a definition-only metric must not be investigated"
    assert "TOTALYTD" in text and "No value can be computed" in text
    assert "definition only" in text.lower() or "not produced" in text.lower()
    # no fabricated figures and no invented citations
    assert "[Q-" not in text
