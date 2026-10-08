# Phase 00 — Foundations

**Goal:** a runnable application with a database, migrations, config, structured logging and an
error hierarchy. No features. This phase exists so that every later failure is in the code you
just wrote, not in the plumbing.

**You can start the next phase when:** the gate at the bottom passes.

---

## Build these files

```
pyproject.toml
run.py
udal/__init__.py
udal/core/config.py
udal/core/db.py
udal/core/migrations.py
udal/core/errors.py
udal/core/logging.py
udal/api/app.py
udal/migrations/001_initial.sql
tests/conftest.py
tests/unit/test_foundations.py
```

---

## `pyproject.toml`

Package `udal`, requires-python `>=3.11`. Core dependencies only (see conventions). Define the
optional extras now even though nothing uses them yet — later phases just fill them in:

```toml
[project.optional-dependencies]
google    = ["google-auth", "google-auth-oauthlib", "google-api-python-client"]
microsoft = ["msal"]
postgres  = ["psycopg[binary]"]
mysql     = ["PyMySQL"]
mssql     = ["pyodbc"]
all       = ["udal[google,microsoft,postgres,mysql,mssql]"]

[project.scripts]
udal = "run:main"
```

---

## `udal/core/config.py`

`pydantic-settings`, env prefix `UDAL_`, loads `.env`.

| Setting | Default | Notes |
|---|---|---|
| `database_path` | `./data/udal.db` | parent directory is created on startup |
| `secret_key` | *required* | used to derive the Fernet key in Phase 02 |
| `host` / `port` | `127.0.0.1` / `8000` | bind to localhost by default, not `0.0.0.0` |
| `log_level` | `INFO` | |
| `max_result_rows` | `10000` | hard cap on any single read |
| `query_timeout_seconds` | `60` | |
| `plugin_paths` | `[]` | extra directories scanned for plugins (Phase 01) |
| `ai_egress_enabled` | `True` | kill switch: `False` blocks all outbound AI calls |

Generate and persist a `secret_key` on first run if absent, writing it to `.env` with a loud log
line. Losing it means losing every stored credential — say so in the log.

Export a cached `get_settings()`.

---

## `udal/core/db.py`

```python
def connect() -> sqlite3.Connection      # applies every PRAGMA from the conventions
def get_db() -> Iterator[sqlite3.Connection]   # FastAPI dependency, closes after request
```

One connection per thread — use `threading.local()`. Apply `WAL`, `foreign_keys=ON`,
`busy_timeout=30000`, `timeout=30`, `row_factory = sqlite3.Row`.

Add a `tx()` context manager that commits on success and rolls back on exception. Every write
path uses it.

---

## `udal/core/migrations.py`

Deliberately not Alembic. Six tables do not justify a migration framework.

```python
def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """Run every unapplied .sql file in udal/migrations/ in filename order.
    Returns the names applied. Idempotent."""
```

- Table `schema_migrations(filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)`.
- Each file runs inside one transaction. A failure rolls back that file and raises — it must
  not leave the database half-migrated.
- Files are applied in sorted filename order, so always prefix with a zero-padded number.
- Called automatically on app startup.

## `udal/migrations/001_initial.sql`

```sql
CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Background work. No Celery: a thread pool writes progress here and the UI polls it.
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
    progress    INTEGER NOT NULL DEFAULT 0,
    detail      TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at DESC);

-- Append-only. Every outbound call to a user system or AI provider lands here.
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    actor      TEXT,
    action     TEXT NOT NULL,
    target     TEXT,
    detail     TEXT,
    ok         INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS audit_log_at ON audit_log(at DESC);
```

---

## `udal/core/logging.py`

One JSON object per line: `ts`, `level`, `event`, plus arbitrary fields.

Install a `logging.Filter` that redacts anything that looks like a secret before it is emitted —
keys matching `password|secret|token|key|credential|authorization` are replaced with `"***"`,
recursively through dicts. This is belt-and-braces behind the rule that secrets are never logged
in the first place.

## `udal/core/errors.py`

Exactly the hierarchy from the conventions file. Nothing more.

---

## `udal/api/app.py`

```python
def create_app() -> FastAPI
```

- Lifespan: create the data directory, connect, `apply_migrations`, log the applied list.
- `GET /health` → `{"status": "ok", "version": ..., "database": "connected", "migrations": N}`
- Exception handlers mapping `UdalError` subclasses to status codes:
  `ConfigError`→500, `AuthError`→401, `ConnectorError`→502, `PolicyError`→400, `LeakError`→500.
  Each returns `{"error": "<class name>", "message": str(exc)}` — and for `LeakError`, a
  deliberately generic message, since the detail names leaked values.
- CORS off for now; Phase 09 turns it on for the unified API only.

## `run.py`

```python
def main() -> None:   # uvicorn.run(create_app(), host=..., port=...)
```

---

## Validation Gate 00

Run each command. All four must produce the stated result.

**1. It installs and starts**

```bash
pip install -e .
python run.py
```

Expect a log line listing applied migrations, then uvicorn listening on 127.0.0.1:8000.

**2. Health check**

```bash
curl -s http://127.0.0.1:8000/health
```

```json
{"status":"ok","version":"0.1.0","database":"connected","migrations":1}
```

**3. Migrations are idempotent and the schema is real**

```bash
python -c "import sqlite3; c=sqlite3.connect('data/udal.db'); print(sorted(r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")))"
```

```
['app_meta', 'audit_log', 'jobs', 'schema_migrations', 'sqlite_sequence']
```

Restart the app. The migration log must say **0 applied** the second time.

**4. Tests pass**

```bash
pytest -q
```

`tests/unit/test_foundations.py` must cover:

- `apply_migrations` twice in a row applies files once and returns `[]` the second time.
- A migration file that raises leaves the database unchanged (write a deliberately broken
  temporary `.sql` and assert no partial table survives).
- `PRAGMA foreign_keys` reports `1` on a fresh connection — it is off by default in SQLite and
  this catches a missing pragma early.
- The logging filter redacts `{"password": "hunter2"}` to `"***"`, including when nested.
- `ConfigError` returns 500 and `AuthError` returns 401 through the app's handlers.

---

## Common failures in this phase

| Symptom | Cause |
|---|---|
| `database is locked` under any load | missing `timeout=30` / `busy_timeout` / WAL |
| Foreign keys silently not enforced | `PRAGMA foreign_keys=ON` must be set **per connection**, not once |
| Migration half-applied after an error | each file must run in its own transaction |
| Credentials unreadable after a restart | `secret_key` regenerated instead of persisted |
| `sqlite3.ProgrammingError` about threads | a connection was shared across threads; use `threading.local()` |
