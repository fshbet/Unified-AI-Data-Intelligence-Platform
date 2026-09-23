import pytest

from backend.query_engine.sql_safety import UnsafeSQL, ensure_limit, referenced_tables, validate_read_only


@pytest.mark.parametrize("sql", [
    "SELECT * FROM sales",
    "  select region, sum(revenue) from sales group by 1 ",
    "WITH t AS (SELECT 1) SELECT * FROM t",
    "SELECT * FROM sales WHERE note = 'DROP TABLE x'",  # keyword inside string literal is fine
    "WITH created AS (SELECT 1) SELECT * FROM created",  # CTE named like a keyword
    "SELECT user_set, asset FROM t",  # identifiers containing keywords
])
def test_allows_read_only(sql):
    assert validate_read_only(sql)


@pytest.mark.parametrize("sql", [
    "DROP TABLE sales",
    "DELETE FROM sales",
    "SELECT 1; DROP TABLE sales",
    "UPDATE sales SET revenue = 0",
    "INSERT INTO sales VALUES (1)",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT pg_sleep(10)",
    "CREATE TABLE x AS SELECT 1",
    "SELECT * FROM t; SELECT 2",
    "",
    "EXEC xp_cmdshell 'dir'",
])
def test_blocks_writes_and_dangerous(sql):
    with pytest.raises(UnsafeSQL):
        validate_read_only(sql)


def test_ensure_limit_per_dialect():
    assert ensure_limit("SELECT * FROM t", 10) == "SELECT * FROM (SELECT * FROM t) AS _q LIMIT 10"
    assert ensure_limit("SELECT * FROM t LIMIT 5", 10) == "SELECT * FROM t LIMIT 5"
    assert ensure_limit("SELECT * FROM t", 10, "mssql").startswith("SELECT TOP 10")
    assert "FETCH FIRST 10" in ensure_limit("SELECT * FROM t", 10, "oracle")


def test_referenced_tables():
    assert referenced_tables("SELECT * FROM finance.invoices i JOIN sales s ON 1=1") == {"finance.invoices", "sales"}
    assert referenced_tables('WITH x AS (SELECT * FROM "hr") SELECT * FROM x') == {"hr"}
