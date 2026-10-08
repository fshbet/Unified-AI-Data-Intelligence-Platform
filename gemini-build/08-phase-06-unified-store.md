# Phase 06 — The Unified Store

**Goal:** many different systems become **one shape in one place**. After this phase, a single
query returns Google Sheets rows, Azure SQL rows and Postgres rows side by side, each carrying
the provenance that says where it came from.

This is the phase that delivers *"attach this app to any application and get all the data in one
place"*. Phase 09 exposes it; this phase builds it.

**Depends on:** Phases 00–03 (04 and 05 optional).

---

## Build these files

```
udal/store/models.py          Record, SyncResult, Lineage
udal/store/ingest.py          connector Page -> canonical rows
udal/store/query.py           the unified query
udal/store/schema_map.py      per-dataset column -> canonical field mapping
udal/store/sync.py            full + incremental sync, driven by the jobs table
udal/migrations/003_store.sql
udal/api/routers/sync.py
tests/integration/test_unified_store.py
```

---

## `udal/migrations/003_store.sql`

```sql
-- One row per dataset pulled from a connection.
CREATE TABLE IF NOT EXISTS datasets (
    id            TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,              -- table / sheet / list name at the source
    entity        TEXT NOT NULL DEFAULT 'other',
    enabled       INTEGER NOT NULL DEFAULT 1,
    row_count     INTEGER,
    last_sync_at  TEXT,
    cursor        TEXT,                       -- incremental watermark
    created_at    TEXT NOT NULL,
    UNIQUE (connection_id, name)
);

CREATE TABLE IF NOT EXISTS dataset_columns (
    dataset_id    TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    type          TEXT NOT NULL DEFAULT 'string',
    native_type   TEXT,
    canonical     TEXT,                       -- e.g. "customer.name"; NULL if unmapped
    nullable      INTEGER NOT NULL DEFAULT 1,
    position      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (dataset_id, name)
);

-- The unified table. Every record from every system lands here, same shape.
CREATE TABLE IF NOT EXISTS records (
    id           TEXT PRIMARY KEY,            -- sha256(dataset_id || natural_key)
    dataset_id   TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    entity       TEXT NOT NULL,
    natural_key  TEXT NOT NULL,
    fields       TEXT NOT NULL,               -- JSON object
    content_hash TEXT NOT NULL,               -- change detection
    fetched_at   TEXT NOT NULL,
    lineage      TEXT NOT NULL DEFAULT '{}',  -- JSON
    deleted_at   TEXT,                        -- soft delete; never hard-delete history
    UNIQUE (dataset_id, natural_key)
);
CREATE INDEX IF NOT EXISTS records_entity  ON records(entity, fetched_at DESC);
CREATE INDEX IF NOT EXISTS records_dataset ON records(dataset_id) WHERE deleted_at IS NULL;

-- Full-text over the JSON, so the unified query can do keyword search with no extra dependency.
CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
    id UNINDEXED, entity UNINDEXED, body, tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id            TEXT PRIMARY KEY,
    dataset_id    TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    mode          TEXT NOT NULL,              -- full | incremental
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    rows_read     INTEGER NOT NULL DEFAULT 0,
    rows_changed  INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL,
    error         TEXT
);
```

SQLite's `json_extract` makes `fields` queryable without a column per attribute, which is what
lets wildly different sources share one table. Index specific JSON paths only when a query
proves slow:

```sql
CREATE INDEX records_customer_email
    ON records(json_extract(fields, '$.email')) WHERE entity = 'customer';
```

---

## `udal/store/ingest.py`

```python
def ingest_page(conn, dataset_id: str, page: Page) -> int:
    """Upsert a connector page into `records`. Returns rows actually changed."""
```

Rules:

1. **`natural_key`** comes from the dataset's declared key columns; fall back to a hash of the
   whole row. Record which was used in `lineage["key_strategy"]` — a hash key means an edit
   looks like a new record, and downstream needs to know that.
2. **`content_hash`** = sha256 of the canonicalised `fields` JSON (sorted keys). If it is
   unchanged, update `fetched_at` only and do not count it as changed. On a 50,000-row sheet
   where three cells moved, this is the difference between 3 writes and 50,000.
3. **Upsert**, never delete-then-insert — `ON CONFLICT(dataset_id, natural_key) DO UPDATE`.
4. Keep the FTS row in step inside the same transaction.
5. Batch with `executemany` in chunks of ~500, one transaction per page.

### Soft deletes

A record absent from a *full* sync is marked `deleted_at`, never removed. Incremental syncs must
not mark anything deleted — they only saw a window. Getting this backwards silently wipes the
store, so make it an explicit parameter, not an inference.

---

## `udal/store/schema_map.py`

Maps a source column onto a canonical field: `customers.cust_email` → `customer.email`.

- Suggest automatically by normalised name and type (`cust_email`, `Email Address`,
  `email_addr` → `email`), with a confidence score.
- **Suggestions are suggestions.** A mapping is applied only when accepted — store
  `accepted_by` and `accepted_at`. Silent automatic mapping across systems is how two different
  people's data gets merged into one record.
- Unmapped columns stay in `fields` under their original name. Nothing is ever dropped.

---

## `udal/store/sync.py`

```python
def sync_dataset(conn, dataset_id: str, mode: str = "incremental") -> SyncResult
def sync_connection(conn, connection_id: str, mode: str = "incremental") -> list[SyncResult]
```

Runs in the Phase 00 thread pool, writing progress to `jobs`. Per the conventions:
**commit before handing work to the pool** and give each worker its own connection, or you will
get `database is locked` under parallel syncs.

Incremental uses the stored `cursor` — a watermark column (`updated_at`), an API delta token, or
a row offset, in that order of preference. Fall back to full sync when no watermark exists and
say so in the run record rather than pretending the sync was incremental.

Rate limiting, retries with exponential backoff and jitter, and a per-run row cap
(`settings.max_result_rows`) all live here, not in connectors.

---

## `udal/store/query.py` — the unified query

```python
def query(
    conn,
    entity: str | None = None,
    sources: list[str] | None = None,
    filters: dict[str, Any] | None = None,   # {"region": "West", "amount__gt": 1000}
    search: str | None = None,               # FTS
    fields: list[str] | None = None,         # projection
    order_by: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> QueryResult
```

Operators: `__eq __ne __gt __gte __lt __lte __in __contains __startswith __isnull`.

**Build SQL with bound parameters only.** Filter keys are validated against the known column set
before being interpolated as JSON paths; a filter name must never reach the SQL string
unchecked. This endpoint will be exposed publicly in Phase 09 — it is the app's primary
injection surface.

`QueryResult` carries `records`, `total`, `next_cursor`, and `sources` — the list of connections
that contributed, with each one's `last_sync_at`. A consumer must be able to see that one of its
five sources is four days stale.

---

## Validation Gate 06

**1. Two different systems, one query**

Connect a SQLite database and a Google Sheet (or the demo connector). Sync both. Then:

```bash
curl -s "http://127.0.0.1:8000/v1/unified/records?entity=customer&limit=5" | python -m json.tool
```

Records from both must appear in one list, one shape, each with `source_type` and `lineage`
identifying its origin. **This is the phase's whole point — do not proceed until you see it.**

**2. Re-sync is cheap and idempotent**

Sync the same unchanged dataset twice:

```python
first  = sync_dataset(conn, ds_id, "full")
second = sync_dataset(conn, ds_id, "full")
assert second.rows_changed == 0
assert count_records(ds_id) == first.rows_read     # no duplicates
```

**3. A changed cell is detected, an unchanged row is not rewritten**

Change one cell in a 1,000-row dataset, re-sync, assert `rows_changed == 1`.

**4. Deletes are soft, and incremental never deletes**

Remove a row at the source, full-sync, assert `deleted_at` is set and the row is still in the
table. Then assert an *incremental* sync of a dataset marks nothing deleted.

**5. Filters, search and paging**

```python
query(conn, entity="customer", filters={"region": "West", "revenue__gt": 1000})
query(conn, search="renewal")                       # FTS
```

Page a 2,500-record entity at 1,000 per page: 2,500 unique ids, `next_cursor is None` at the end.

**6. Injection is not possible**

```python
query(conn, filters={"region'; DROP TABLE records; --": "x"})
```

Must raise a validation error. Then assert `records` still exists. Add a filter name with a
quote, a JSON-path traversal (`$.a.b`), and a Unicode lookalike quote to the same test.

**7. Staleness is visible**

`QueryResult.sources` reports `last_sync_at` per contributing connection.

**8. Parallel syncs do not lock**

Sync three connections concurrently. All succeed; no `database is locked`.

---

## Common failures in this phase

| Symptom | Cause |
|---|---|
| Every sync rewrites every row | `content_hash` computed over unsorted JSON, so it never matches |
| Duplicates after re-sync | `natural_key` unstable (row number as key on a re-ordered sheet) |
| Store empties itself | incremental sync applying full-sync delete semantics |
| `database is locked` | workers sharing a connection, or a write txn held across network I/O |
| Unified query slow at 100k rows | missing index on a hot `json_extract` path |
| Two people merged into one record | a schema mapping auto-applied without acceptance |
