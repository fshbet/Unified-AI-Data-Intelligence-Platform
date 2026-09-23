"""BI formula translation. The critical property is that anything not translatable *exactly*
is refused, because a silently-wrong measure would poison every answer that cites it."""
import pytest

from backend.semantic.expressions import demo, translate


def test_builtin_self_check():
    demo()


@pytest.mark.parametrize("formula,sql", [
    ("SUM('Sales'[Revenue])", 'SUM("Revenue")'),
    ("sum('Sales'[Revenue])", 'SUM("Revenue")'),
    ("AVERAGE('Sales'[Price])", 'AVG("Price")'),
    ("MIN('Sales'[Qty])", 'MIN("Qty")'),
    ("COUNTA('Sales'[Note])", 'COUNT("Note")'),
    ("SUM(Sales[Qty])", 'SUM("Qty")'),  # unquoted table syntax
    ("SUM('Sales'[A]) + SUM('Sales'[B]) - SUM('Sales'[C])", 'SUM("A") + SUM("B") - SUM("C")'),
    ("ABS(SUM('Sales'[Delta]))", 'ABS(SUM("Delta"))'),
    ("// leading comment\nSUM('Sales'[Revenue])", 'SUM("Revenue")'),
])
def test_translatable_dax(formula, sql):
    t = translate(formula, "dax")
    assert t.ok, t.reason
    assert t.sql == sql


@pytest.mark.parametrize("formula", [
    "CALCULATE(SUM('S'[Revenue]), FILTER('S', 'S'[Qty] > 1))",   # FILTER changes row context
    "CALCULATE(SUM('S'[Revenue]), ALLEXCEPT('S', 'S'[Region]))",
    "SAMEPERIODLASTYEAR('Date'[Date])",
    "IF(SUM('S'[Qty]) > 0, 1, 0)",                                # IF is not in the allow-list
    "SUM('S'[Revenue]) + [Missing Measure]",
    "VAR x = 1 RETURN x",
    "'S'[Revenue]",                                               # a bare column is not a measure
    "Sales[Revenue] * 0 + SUM(Sales[Qty])",                       # unaggregated column mixed with an aggregate
    "12345",
    "",
])
def test_refused_dax(formula):
    t = translate(formula, "dax")
    assert not t.ok and t.reason, f"{formula!r} should not have translated to {t.sql!r}"


def test_division_guards_against_divide_by_zero():
    t = translate("SUM('S'[Profit]) / SUM('S'[Revenue])", "dax")
    assert t.ok and "NULLIF" in t.sql


def test_calculate_filters_are_extracted_not_dropped():
    t = translate("CALCULATE(SUM('I'[Amt]), 'I'[Status] = \"POSTED\", 'I'[Year] = 2026)", "dax")
    assert t.ok
    assert t.filters == "\"Status\" = 'POSTED' AND \"Year\" = 2026"
    # the filter must never be silently lost — that is the dangerous failure mode
    assert "POSTED" not in t.sql


def test_string_literals_are_escaped():
    t = translate("CALCULATE(SUM('I'[Amt]), 'I'[Name] = \"O'Brien\")", "dax")
    assert t.ok and t.filters == "\"Name\" = 'O''Brien'"


def test_multi_table_measure_is_refused():
    t = translate("DIVIDE(SUM('Sales'[Revenue]), SUM('Budget'[Target]))", "dax")
    assert not t.ok and "multiple tables" in t.reason


def test_nested_measure_cycle_is_refused():
    t = translate("[A]", "dax", {"a": "[B]", "b": "[A]"})
    assert not t.ok


@pytest.mark.parametrize("formula,sql", [
    ("SUM([Sales])", 'SUM("Sales")'),
    ("COUNTD([Customer ID])", 'COUNT(DISTINCT "Customer ID")'),
    ("ZN(SUM([Profit]))", 'COALESCE(SUM("Profit"), 0)'),
    ("SUM([Profit]) / SUM([Sales])", 'SUM("Profit") / NULLIF(SUM("Sales"), 0)'),
])
def test_translatable_tableau(formula, sql):
    t = translate(formula, "tableau")
    assert t.ok, t.reason
    assert t.sql == sql


@pytest.mark.parametrize("formula", [
    "WINDOW_AVG(SUM([Sales]))",
    "RUNNING_SUM(SUM([Sales]))",
    "INDEX()",
    "[Price] * [Qty]",          # row level, not an aggregate
])
def test_refused_tableau(formula):
    t = translate(formula, "tableau")
    assert not t.ok and t.reason
