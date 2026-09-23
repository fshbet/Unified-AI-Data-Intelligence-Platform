"""Profiling, PII classification, schema inference, period parsing, dialects, versioning, vector search."""
from datetime import date

import pandas as pd

from backend.metadata.pii import classify_column, mask_value
from backend.metadata.profiler import profile_column, profile_table
from backend.query_engine.dialects import get_dialect
from backend.query_engine.periods import comparison_for, default_period, resolve_period, shift
from backend.semantic.relationships import canonical_stem, key_stem, name_similarity
from backend.vector_store.store import _bm25, tokenize


def test_profile_numeric_column():
    p = profile_column(pd.Series([1, 2, 3, 4, 100, None], name="revenue_amt"))
    assert p["logical_type"] == "number"
    assert p["semantic_type"] == "measure"
    st = p["stats"]
    assert st["null_count"] == 1 and st["distinct_count"] == 5 and st["min"] == 1 and st["max"] == 100
    assert st["median"] == 3 and st["outlier_count"] == 1


def test_profile_detects_dates_and_dimensions():
    df = pd.DataFrame({"order_date": ["2026-01-01", "2026-02-01", "2026-03-15"] * 10, "region": ["North", "South", "West"] * 10, "order_id": [f"O{i}" for i in range(30)]})
    p = profile_table(df)
    assert p["date_column"] == "order_date"
    assert p["columns"]["order_date"]["logical_type"] == "date"
    assert p["columns"]["region"]["semantic_type"] == "dimension"
    assert p["columns"]["order_id"]["semantic_type"] == "identifier" and p["columns"]["order_id"]["stats"]["is_unique"]


def test_pii_classification():
    assert classify_column("email", [])["pii_type"] == "email"
    assert classify_column("contact", ["a@b.com", "c@d.org", "e@f.net"])["pii_type"] == "email"
    assert classify_column("pan_number", [])["sensitivity"] == "restricted"
    assert classify_column("salary", [])["sensitivity"] == "sensitive"
    assert classify_column("revenue", ["1", "2", "3"])["pii_type"] is None
    assert classify_column("card", ["4111111111111111", "4012888888881881", "5555555555554444"])["pii_type"] == "credit_card"
    assert mask_value("john.doe@example.com", "email") == "j***@example.com"
    assert mask_value("9876543210", "phone") == "98******10"


def test_period_resolution():
    anchor = date(2026, 10, 4)
    assert resolve_period("why did revenue drop in August?", anchor).label == "August 2026"
    assert resolve_period("compare Q2 2026", anchor).label == "Q2 2026"
    assert resolve_period("what happened in 2026-03", anchor).key == "2026-03"
    assert default_period(anchor).label == "September 2026"  # October is partial
    assert resolve_period("last month", anchor).label == "August 2026"
    assert resolve_period("last quarter", anchor).label == "Q3 2026"
    p = resolve_period("August", anchor)
    assert comparison_for("vs last year", p).label == "August 2025"
    assert comparison_for("", p).label == "July 2026"
    assert shift(p, -1).label == "July 2026" and shift(p, 5).label == "January 2027"


def test_dialect_sql():
    assert "DATE_TRUNC('month'" in get_dialect("postgresql").month_trunc("d")
    assert "DATEFROMPARTS" in get_dialect("mssql").month_trunc("d")
    assert "DATE_FORMAT" in get_dialect("mysql").month_key("d")
    assert get_dialect("mssql").table("orders", "dbo") == "[dbo].[orders]"
    assert get_dialect("mysql").ident("x") == "`x`"
    assert "TOP 5" in get_dialect("mssql").limit("SELECT 1", 5)


def test_relationship_name_heuristics():
    assert key_stem("customer_id") == "customer" and key_stem("CustomerCode") == "customer" and key_stem("region") is None
    assert canonical_stem("cust") == "customer" and canonical_stem("emp") == "employee"
    assert name_similarity("customer_id", "customer_code") == 1.0
    assert name_similarity("client_id", "customer_id") == 1.0
    assert name_similarity("product_id", "employee_id") < 0.6


def test_bm25_ranks_relevant_first():
    docs = ["column sales.revenue measure number domain sales", "column hr.exit_date employee separation date", "metric attrition rate employees leaving"]
    s = _bm25("employee attrition", docs)
    assert s.argmax() == 2
    assert tokenize("revenue_amt CustomerCode") == ["revenue", "amt", "customer", "code"]
