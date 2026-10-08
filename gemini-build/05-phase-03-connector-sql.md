# Phase 03 — SQL Connectors

**Goal:** the first real data in the system, and the read-only guarantee that makes it safe to
point this app at a production database.

**Depends on:** Phases 00–02.

---

## Build these files

```
udal/connectors/safety.py                 read-only SQL validation
udal/connectors/dialects.py               per-engine quirks
udal/plugins/connectors/sql_connector.py  sqlite|postgresql|mysql|mssql
tests/unit/test_sql_safety.py
tests/integration/test_sql_connector.py
```

---

## `udal/connectors/safety.py` — the read-only boundary

Every SQL string that will touch a user's database passes through here first. No exceptions, no
"trusted internal caller" path.

```python
def validate_read_only(sql: str) -> None:
    """Raise ConnectorError unless `sql` is a single, read-only statement."""

def ensure_limit(sql: str, limit: int, dialect: str) -> str:
    """Append/replace a row limit appropriate to the dialect."""
```

Rules, in order:

1. Strip comments first (`--` to end of line, `/* ... */`). **Do this before anything else** —
   `SELECT 1; /*`-style comment tricks are how keyword checks get bypassed.
2. Reject if more than one statement remains (split on `;`, ignoring semicolons inside string
   literals; a trailing `;` is fine).
3. The first keyword must be `SELECT` or `WITH`.
4. Reject these anywhere as whole words, case-insensitive:
   `INSERT UPDATE DELETE DROP ALTER TRUNCATE CREATE REPLACE MERGE GRANT REVOKE
   ATTACH DETACH PRAGMA VACUUM EXEC EXECUTE CALL INTO`
5. Reject file and network functions by name:
   `load_file into outfile into dumpfile pg_read_file pg_ls_dir lo_import lo_export
   copy xp_cmdshell openrowset opendatasource readfile sqlite3_load_extension`

Use whole-word matching. A column legitimately named `created_at` must not trip the `CREATE`
rule — the test suite checks exactly this.

> **Defence in depth, not the only defence.** Also connect with a read-only user wherever the
> engine allows it, and set `PRAGMA query_only=ON` for SQLite. The validator is the last line,
> not the first.

### `ensure_limit`

| Dialect | Form |
|---|---|
| sqlite, postgresql, mysql | `SELECT ... LIMIT n` |
| mssql | `SELECT TOP (n) ...` — inserted after `SELECT`/`SELECT DISTINCT` |
| oracle | `... FETCH FIRST n ROWS ONLY` |

If a limit is already present and is lower than the cap, leave it alone.

---

## `udal/connectors/dialects.py`

One small table of differences so the connector body stays uniform:

```python
@dataclass(frozen=True)
class Dialect:
    name: str
    quote: Callable[[str], str]            # identifier quoting: "x" vs `x` vs [x]
    list_tables_sql: str
    list_columns_sql: str
    row_count_sql: str
    limit_style: str                       # "limit" | "top" | "fetch"
    default_port: int
```

`quote` matters more than it looks: a sheet or table called `Order Details` or `select` breaks
every unquoted query, and those names are everywhere in real data.

---

## `udal/plugins/connectors/sql_connector.py`

One connector, four engines, selected by a `engine` config field.

```python
@register
class SqlConnector(Connector):
    manifest = Manifest(
        key="sql", kind="connector", display_name="SQL Database",
        config_fields=[
            Field("engine", "Engine", type="select",
                  choices=["sqlite", "postgresql", "mysql", "mssql"]),
            Field("host", "Host", required=False),
            Field("port", "Port", type="int", required=False),
            Field("database", "Database"),
            Field("schema", "Schema", required=False, default="public"),
            Field("sslmode", "SSL mode", type="select", required=False,
                  choices=["prefer", "require", "disable"]),
            Field("file_path", "File path", required=False, help="SQLite only"),
        ],
        requires=["postgres|mysql|mssql depending on engine"],
        capabilities={"read_only": True, "paged": True, "sql": True},
    )
    supported_auth = ("credentials", "ambient", "service_account")
```

Auth modes map to:

- **`credentials`** — username + password, or a full DSN. The common case.
- **`ambient`** — integrated/trusted auth: Windows auth for SQL Server, peer/ident for Postgres,
  the instance's managed identity for a cloud database.
- **`service_account`** — Azure AD token for Azure SQL, or an IAM token for Cloud SQL. The
  credential arrives as a bearer token and is passed as the password in the connection string;
  that is genuinely how both of those work.

### Required methods

```python
def test(self) -> TestResult        # connect, SELECT 1, report server version
def discover(self) -> list[DatasetInfo]
def read(self, dataset, cursor=None, limit=1000) -> Page
def read_sql(self, sql, params=None) -> Page   # ad-hoc, still read-only validated
```

`discover()` reads `information_schema` (or `sqlite_master`) for tables, columns and types, and
an approximate row count — use the engine's statistics view, not `COUNT(*)`, which can take
minutes on a large table.

### Pagination — keyset, not OFFSET

```python
# Preferred: a stable ordering column (primary key or rowid)
SELECT ... FROM "t" WHERE "id" > :cursor ORDER BY "id" LIMIT :n
```

`OFFSET` degrades quadratically and silently skips or repeats rows when the table is being
written to. Fall back to `OFFSET` only when no single stable ordering column exists, and record
`lineage["pagination"] = "offset"` so the imprecision is visible downstream rather than assumed
away.

Set a statement timeout per engine (`statement_timeout`, `max_execution_time`,
`LOCK_TIMEOUT`/query governor) from `settings.query_timeout_seconds`.

### Type mapping

Normalise engine types to the `ColumnInfo` vocabulary (`string|int|float|bool|date|datetime|json`).
Keep the original in `ColumnInfo.native_type` — it is needed for sensible anonymisation defaults
in Phase 07 (a `date` column should default to `shift`, not `pseudonym`).

---

## Validation Gate 03

**1. The safety validator refuses everything it should**

```bash
pytest -q tests/unit/test_sql_safety.py
```

Must accept:

```sql
SELECT * FROM customers
WITH recent AS (SELECT * FROM orders) SELECT * FROM recent
select created_at, update_count from audit     -- column names containing keywords
SELECT 'DROP TABLE x' AS literal_text          -- keyword inside a string literal
```

Must reject, each with a clear message:

```sql
DROP TABLE customers
SELECT 1; DROP TABLE customers
SELECT * FROM t -- harmless
;DROP TABLE t
SELECT load_file('/etc/passwd')
SELECT * INTO OUTFILE '/tmp/x' FROM t
INSERT INTO t VALUES (1)
PRAGMA table_info(t)
SELECT * FROM t /* */; DELETE FROM t
```

That last pair is the one naive implementations fail. Strip comments before counting statements.

**2. End-to-end against a real SQLite file**

```bash
python -c "
from udal.plugins.connectors.sql_connector import SqlConnector
c = SqlConnector({'engine':'sqlite','file_path':'tests/fixtures/sample.db'})
print(c.test().ok)
ds = c.discover(); print([d.name for d in ds])
p = c.read(ds[0].name, limit=2); print(len(p.records), p.next_cursor is not None)
"
```

**3. Writes are impossible through every path**

```python
with pytest.raises(ConnectorError):
    connector.read_sql("UPDATE customers SET name='x'")
```

Then confirm the table is untouched. Do the same through the HTTP API, not just the Python
object — a route that forgets to validate is the realistic failure.

**4. Quoted identifiers survive**

Create a table named `Order Details` with a column `select`. `discover()` and `read()` must both
work. This is the single most common real-world break.

**5. Pagination is correct**

Read a 2,500-row table in pages of 1,000. Assert exactly 2,500 unique rows, no duplicates, no
gaps, and `next_cursor is None` on the final page.

**6. Postgres / MySQL / SQL Server**

Optional but recommended if you have them. Mark the tests `skipif` on an env var holding a DSN,
so they are skipped cleanly rather than failing on a machine without a server:

```bash
UDAL_TEST_PG_URL=postgresql://user:pass@localhost:5432/test pytest -q tests/integration/test_sql_connector.py
```

---

## Common failures in this phase

| Symptom | Cause |
|---|---|
| Safety check rejects a valid query | keyword matched as a substring; use whole-word matching |
| Safety check accepts a stacked statement | comments not stripped before splitting on `;` |
| `discover()` takes minutes | `COUNT(*)` per table instead of the statistics view |
| Rows duplicated or missing across pages | `OFFSET` on a table being written to; use keyset |
| Breaks on one table only | unquoted identifier with a space or reserved word |
| Connection pool exhausted during a sync | connections opened per page and never closed |
