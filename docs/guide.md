# User & Operator Guide

## Installation

**Requirements:** Python 3.11+ (tested on 3.12/3.14), Node 20+, optionally Docker.

```bash
python -m venv .venv
.venv/Scripts/pip install -r backend/requirements.txt        # or .venv/bin/pip
python scripts/generate_demo_data.py
python scripts/seed_demo.py --reset
.venv/Scripts/python -m uvicorn backend.main:app --port 8000
cd frontend && npm install && npm run dev
```

The backend creates tables on start (`Base.metadata.create_all`) and a default admin. For production use
`alembic upgrade head` (see `alembic.ini`, `migrations/`).

## Configuration

All settings are environment variables prefixed `EDI_` (see `.env.example`, `backend/core/config.py`):

| Variable | Purpose |
|---|---|
| `EDI_DATABASE_URL` | Metadata catalog (`sqlite:///…` or `postgresql+psycopg2://…`) |
| `EDI_SECRET_KEY` | JWT signing; also derives the Fernet key that encrypts stored credentials/API keys |
| `EDI_ENCRYPTION_KEY` | Optional explicit Fernet key |
| `EDI_QUERY_TIMEOUT_SECONDS`, `EDI_MAX_RESULT_ROWS`, `EDI_PROFILE_SAMPLE_ROWS` | Query safety and profiling limits |
| `EDI_AI_MAX_TOOL_ITERATIONS`, `EDI_AI_REQUEST_TIMEOUT_SECONDS` | LLM loop limits |
| `EDI_SCHEDULER_ENABLED`, `EDI_SCHEDULER_TICK_SECONDS` | Scheduled refresh |
| `NEXT_PUBLIC_API_URL` | Frontend → API base URL |

## Data sources

**Data Sources → Add data source.** Pick a type; the form is generated from the connector's `config_fields`.
Test the connection, then **Add & sync**: discover schema → profile tables → classify PII → run quality
checks → suggest relationships/entities → index the semantic catalog. Each source has an owner, department,
description, refresh frequency, read-only flag, enable/disable, health and last-sync time.

* **Files** — upload CSV/TSV/Excel (each sheet a table)/JSON/JSONL/Parquet/XML. Large CSV/Parquet/JSON are
  read by DuckDB directly (streamed, bounded memory).
* **Databases** — host/port/database/user/password/SSL/schema. Only `SELECT` is ever executed.
* **Redis** — keys are scanned by pattern; hashes and JSON strings become one table per key prefix
  (`order:*` → `order`), everything else lands in `redis_keys`.
* **MongoDB** — collections flattened (nested keys → `parent_child` columns).
* **REST / GraphQL** — URL, method, auth (bearer/basic/API-key header), headers, params, response path,
  pagination (page/offset/cursor); the response schema is detected automatically.
* **Power BI semantic model** — dataset (semantic model) ID plus an Entra ID service principal or a
  pasted access token. Uses the REST API only, so no gateway or .NET runtime is needed. Reads the model
  through `INFO.VIEW.*` DAX functions and pulls each table's rows with `EVALUATE TOPN(...)`.
* **Tableau Server / Cloud** — server URL, site, and a personal access token (or username/password).
  The Metadata API supplies the published datasource's fields and calculated-field formulas; the VizQL
  Data Service supplies the rows. For a local server with a self-signed certificate, turn off
  *Verify TLS certificate*; on servers older than 2024.2 enable *Metadata only* to import the model
  without row data.

## Datasets & tables

**Datasets** lists every dataset/table with domain, rows, time axis and quality. A table page shows columns
(type, semantic type, description, sensitivity, nulls, distinct), the full statistical profile per column,
sample data (masked per your role), relationships, quality issues, row-count history and governed metrics.
Edit business names/definitions/semantic types/units/sensitivity — every edit creates a semantic-model version.
**AI suggest descriptions** asks the configured `metadata` model; suggestions are shown inline and must be accepted.

## Semantic layer

* **Entities** — canonical entities (Customer, Employee, Product, Order) mapped to the physical identifier
  columns of each system (`customer_id`, `customer_code`, `client_id` …). Auto-suggest groups identifier
  columns by stem/alias; mappings can be added/removed manually.
* **Search** — hybrid lexical (BM25) + vector search over tables, columns, metrics, glossary, relationships.
* **Versions** — every change to metrics, column definitions, relationships, glossary terms, tables, entities.

## Imported BI semantic models

Power BI and Tableau already hold governed definitions, so the platform imports them rather than
re-deriving them. On every sync of such a source:

| From the BI model | Becomes |
|---|---|
| table & field descriptions | table descriptions and column business definitions |
| measures (DAX / Tableau formulas) | governed **Metrics** |
| declared relationships | **approved** relationships (the BI model is authoritative) |
| format strings | metric unit and format (currency / percent) |

Measure formulas are translated to SQL by a parser, not by pattern matching. `SUM('Sales'[Revenue])`
becomes `SUM("Revenue")`; `CALCULATE(SUM('Invoices'[Amount]), 'Invoices'[Status] = "POSTED")` becomes
`SUM("Amount")` with the filter `"Status" = 'POSTED'` preserved separately; references between
measures are inlined.

Anything that cannot be translated **exactly** — time intelligence (`TOTALYTD`, `SAMEPERIODLASTYEAR`),
context transitions (`ALL`, `ALLEXCEPT`, `FILTER`), iterators (`SUMX`), table-spanning formulas,
Tableau window functions — is imported as a **definition-only** metric. It keeps its native formula,
stays searchable and quotable, shows a `definition only` badge in the UI, and the engine refuses to
compute a value for it. The AI is told the same thing and must explain the definition instead of
estimating a number. This is deliberate: a silently mistranslated measure would produce a confident
wrong answer, which is the failure mode the whole platform exists to prevent.

Re-importing is idempotent (`POST /api/sources/{id}/import-semantic-model`), and
`GET /api/sources/{id}/semantic-model` returns the model as read from the platform.

**Try it without a tenant.** `python scripts/bi_demo_stubs.py` starts local stand-ins for both
platforms that speak the real wire protocols and serve a small model containing both translatable
and deliberately untranslatable measures. It prints the exact connection settings to paste into
*Add data source*. The same stubs back the integration tests, so the demo cannot drift from what is
covered.

## Business glossary & metrics

Glossary terms carry definition, domain, owner, synonyms (used by the planner to recognise metrics in
questions), related terms and business rules. Metrics are governed formulas: aggregate expression, filters,
date column, unit/format, direction, dimensions for decomposition, related metrics. Use
`{period_start}`/`{period_end}` in filters for point-in-time metrics (headcount). **Preview** computes the
metric for the latest complete month.

## Relationships

**Discover relationships** compares identifier columns across all sources using declared FKs, name similarity
(with aliases such as cust→customer, sku→product), type compatibility, cardinality and sampled value overlap;
suggestions carry a confidence and reason and must be **approved** before the AI traverses them. The
interactive graph shows tables coloured by domain; dashed edges are entity-resolution links.

## Data quality

Rules: missing values (optional event columns such as `exit_date` are downgraded), duplicate keys, duplicate
rows, outliers (IQR), invalid negatives on amount/quantity columns, inconsistent category spellings, volume
change vs previous ingestion (±30%), schema drift, stale data. Issues can be acknowledged/resolved and are
attached to every AI answer's confidence.

## AI assistant

Ask in natural language. The progress panel shows the plan, the investigation status and every tool call.
Answers are structured (Executive Summary, Key Findings, Cross-Domain Evidence, Likely Contributors,
Recommended Investigation, Confidence). Every number cites a `Q-…` reference — click it to see the SQL,
source, tables and result rows. Tabs: **Evidence** (kind + strength + source), **Charts**, **SQL**,
**Sources & tables** (with freshness and open quality issues), **Plan & reasoning** (intent, steps, reasoning
path, tool calls, tokens). Follow-up questions keep analytical context (“Was it because of attrition?”,
“Which region?”, “Compare with last year”). Conversations can be saved, shared by link and exported.

## Insights

**Run insight engine** scans every governed metric's monthly series for month-over-month shocks (≥10%) and
z-score anomalies, pre-investigates each across connected domains and surfaces critical quality issues.
**Investigate with AI** opens the assistant with the right question.

## Administration

* **AI Providers** — provider, model, API key (encrypted), base URL, temperature, max tokens, embedding model,
  per-purpose assignment (metadata / embeddings / planning / reasoning / summarization), cost per 1M tokens,
  **Test connection**. Ollama: base URL `http://localhost:11434/v1`, no key.
* **AI Usage & Cost** — requests, tokens, estimated cost by day/model, most expensive query, recent requests.
* **Users** — roles `admin` (everything), `analyst` (edit metadata, query permitted data), `viewer`
  (read-only, PII masked, restricted columns hidden).
* **Permissions** — dataset/table/column allow/deny per role, row-level SQL filters, masking toggle.
  Once a role has an allow rule it becomes default-deny. The assistant is bound by the asking user's rules.
* **Monitoring** — query volume/latency (p50/p95), errors, AI latency/tokens, jobs, connector health.

## Security notes

* Every generated/manual query passes `sql_safety.validate_read_only` (single statement, SELECT/WITH only,
  blocked keywords incl. file/network functions) and `ensure_limit`; timeouts are set per dialect.
* Credentials and API keys are encrypted at rest (Fernet). Secrets are masked in API responses.
* Row-level filters are injected as filtered sub-selects; PII columns are masked in results for non-admins.
* All logins, edits, permission changes, questions and query executions are written to the audit log.
* The Python sandbox runs in a subprocess with an import whitelist, no `open`/`exec`, no environment,
  a wall-clock timeout and a result-size cap.

## Adding things

* **Connector** — create `backend/connectors/<name>.py`, subclass `DataConnector` (live SQL) or
  `MaterializedConnector` (produce DataFrames), set `type_key`, `display_name`, `category`, `dialect`,
  `config_fields`, decorate with `@register`. It appears in the UI automatically.
* **Metric** — Metrics → Define metric (or `POST /api/semantic/metrics`).
* **Business rule** — Glossary term → rules (free text / SQL) or a metric's `filters`.
* **Dialect** — extend `query_engine/dialects.py` (`month_key`, `date_between`, `limit`, `hints`).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Assistant answers deterministically | No AI provider configured or provider test fails — check Administration → AI Providers |
| Ollama slow / truncated | Use a tool-capable model (`qwen3:8b`+), raise `num_ctx` in provider *extra*, keep `EDI_AI_REQUEST_TIMEOUT_SECONDS` high |
| `database is locked` (SQLite) | Use PostgreSQL for multi-user deployments |
| A Power BI measure shows `definition only` | Its DAX has no exact SQL equivalent (time intelligence, `SUMX`, context transition). The formula is preserved; define an equivalent governed metric if you need the number. |
| Power BI returns 401 | The service principal needs the workspace assigned and *Allow service principals to use Power BI APIs* enabled in the tenant settings. |
| Tableau rows are empty | VizQL Data Service needs Tableau 2024.2+; enable *Metadata only* to import just the model, or upgrade. |
| Tableau TLS error on a local server | Turn off *Verify TLS certificate* on the source (self-signed certificate). |
| Source shows `error` | Open the source → Test connection; error text is shown in Health and Monitoring |
| Investigation shows no related metrics | Approve relationships / add entity mappings; define metrics on the related tables with a date column |
| Windows console `UnicodeEncodeError` in scripts | `set PYTHONIOENCODING=utf-8` |
| Port 8000 already in use (another app / Docker container) | Run uvicorn on another port (e.g. `--port 8010`) and set `NEXT_PUBLIC_API_URL=http://localhost:8010/api` in `frontend/.env.local` |

## API

OpenAPI at `http://localhost:8000/api/docs`. Main groups: `/api/auth`, `/api/sources`, `/api/catalog`,
`/api/semantic` (metrics, glossary, entities, relationships, graph, versions, search), `/api/assistant`
(ask = SSE, conversations, sql, insights), `/api/admin` (ai providers, usage, users, permissions, audit,
queries, jobs, quality, dashboard, monitoring).
