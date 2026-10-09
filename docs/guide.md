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

The backend creates tables on start (`Base.metadata.create_all`) and an admin account. On a first run the
admin password is **generated and printed once** to the log; set `EDI_DEFAULT_ADMIN_PASSWORD` beforehand to
choose your own.

For production run `alembic upgrade head` (see `alembic.ini`, `migrations/`). **Migrate before starting the
new code**: `create_all` adds missing tables but cannot alter existing ones, so starting first leaves the
catalog mid-upgrade and every scheduled sync fails until the migration runs. The migrations tolerate that
ordering, but it is still the wrong way round.

Upgrading an existing install also needs a one-off secret migration — see [security.md](security.md).

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
| `EDI_ENVIRONMENT` | `development` or `production`. In production the app **refuses to start** on a default secret key, a default admin password or a localhost CORS origin. |
| `EDI_DEFAULT_ADMIN_PASSWORD` | Blank generates one on first run and prints it once to the log |
| `EDI_PYTHON_ANALYSIS_ENABLED` | Off by default. Runs model-authored Python — see [security.md](security.md) |
| `EDI_QUERY_PREVIEW_RETENTION_DAYS`, `EDI_ANON_VAULT_RETENTION_HOURS`, `EDI_AUDIT_RETENTION_DAYS` | Data retention sweeps |
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
* **Google Sheets** — spreadsheet ID; one table per tab. Blank header cells are *named* rather than
  dropped (dropping one shifts every later column left), duplicates get a suffix, and a column is only
  typed when every value parses — one stray `n/a` keeps it text.
* **Google BigQuery** — project and dataset. Every query is capped by `maximumBytesBilled` (default 1 GB)
  and routed through the same read-only validator as any other SQL.
* **Excel on OneDrive / SharePoint** — workbook path plus optional drive or site ID, read through
  Microsoft Graph with the same header handling as Sheets.
* **SharePoint lists** — site ID; internal field names (`OData__x0020_Cost`) are mapped back to their
  display names, and `@odata.nextLink` is followed verbatim so paging works.
* **Power BI semantic model** — dataset (semantic model) ID plus an Entra ID service principal or a
  pasted access token. Uses the REST API only, so no gateway or .NET runtime is needed. Reads the model
  through `INFO.VIEW.*` DAX functions and pulls each table's rows with `EVALUATE TOPN(...)`.
* **Tableau Server / Cloud** — server URL, site, and a personal access token (or username/password).
  The Metadata API supplies the published datasource's fields and calculated-field formulas; the VizQL
  Data Service supplies the rows. For a local server with a self-signed certificate, turn off
  *Verify TLS certificate*; on servers older than 2024.2 enable *Metadata only* to import the model
  without row data.

## Authentication for a source

A source's credentials are a separate choice from its type. Pick a **connector**, then an **authentication
mode**; the form shows only what that mode needs. Every connector declares which modes it accepts and
implements none of them, so the same mode works the same way everywhere.

| Mode | What you supply | Use it for |
|---|---|---|
| **Stored credentials** | username/password, a DSN, or an API key | databases and plain REST APIs — the common case |
| **Service account** | Google service-account JSON, or an Entra tenant/client/secret | unattended scheduled syncs |
| **Sign in with a browser** | one click; the connection then acts as *you* | "connect my Google account", Microsoft delegated access |
| **Device code** | a short code typed on your phone | a headless server with no browser |
| **Ambient / managed identity** | nothing | running on GCP or Azure — no secret is stored at all |

Things worth knowing before you start:

* **A Google service account is a separate identity.** Sharing the spreadsheet or dataset with its
  `…@…iam.gserviceaccount.com` address is a required step. If it is missed the connection authenticates
  fine and then reports "not found" — the error names the address to share with.
* **Microsoft application vs delegated permissions.** A service principal uses *application* permissions
  and needs admin consent; browser sign-in uses *delegated* permissions and sees only what you can see. A
  connection that works signed-in and 403s unattended is almost always missing admin consent, and the
  error says so.
* Browser and device-code flows need re-authorising if the refresh token is revoked. The source is marked
  **needs auth** and the UI offers a reconnect button rather than failing silently.
* Credentials are encrypted at rest, masked in every API response, and refreshed transparently — ten
  parallel table syncs cause one token refresh, not ten.

## AI privacy

**AI Privacy** in the sidebar controls what the model is allowed to see. This is separate from
*Administration → Permissions*, which controls what **people** see: someone entitled to view every row
can still have the model receive only tokens.

Each column has a treatment:

| Strategy | Reversible | The model receives | Use for |
|---|---|---|---|
| `passthrough` | n/a | the real value | anything not sensitive — **including numbers you want analysed** |
| `pseudonym` | **yes** | `[[PER_0001]]` | names, emails, IDs — anything the answer may refer back to |
| `redact` | no | `[[REDACTED]]` | card numbers, national IDs — no analytical use |
| `mask` | no | `jo****` | when a prefix is genuinely useful |
| `hash` | no | a stable fingerprint | join keys that must match but never be read |
| `generalize` | no | `90000-100000` | salary bands — keeps the magnitude |
| `shift` | no | a date moved by a constant | dates where the **interval** matters |

Defaults come from the PII classifier that already runs during profiling, so email, phone, name, address,
Aadhaar, PAN, national ID and salary columns are protected from the first sync. They are shown as
*auto · unconfirmed* until you confirm them — **Confirm N detected** accepts them in bulk.

The **What the AI receives** column is a live preview produced by the same code path as a real request, so
it cannot drift from reality. Sample values are masked on this page too: a privacy screen should not
display the data it protects.

Every answer carries a badge showing how many values were withheld. If the model references a token that
was never in the data, the answer is flagged as unverified rather than printing a dangling token.

`docs/security.md` documents the limits, including that values under three characters are not covered by
the outbound guard and that webhooks send real data by design.

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
* **Values from protected columns never reach an AI provider** — see *AI privacy* above. An egress guard
  checks the serialised request body immediately before the HTTP call and aborts if an original survives.
* Stored result rows, the de-anonymisation vault and expired auth handshakes are purged on a timer
  (`EDI_QUERY_PREVIEW_RETENTION_DAYS`, `EDI_ANON_VAULT_RETENTION_HOURS`, `EDI_AUDIT_RETENTION_DAYS`).
* **Python analysis is disabled by default** (`EDI_PYTHON_ANALYSIS_ENABLED=false`). It executes
  model-authored code, and the subprocess guard — import allow-list, stripped builtins, timeout — is a
  speed bump rather than a sandbox: attribute traversal reaches `subprocess.Popen`. Enable it only inside
  an isolated container. While disabled the tool is not offered to the model at all.
* With `EDI_ENVIRONMENT=production` the app refuses to start on a default secret key, a default admin
  password, or a localhost CORS origin.

**[security.md](security.md) is the authoritative document** — it states the limits of the privacy
boundary as plainly as its protections, and carries the pre-deployment checklist.

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
| Google source authenticates then says "not found" | The sheet or dataset is not shared with the service account; the error names the address to share with |
| Microsoft source works signed-in but 403s unattended | The app registration has no admin consent for its *application* permissions |
| Source shows `needs auth` | A refresh token expired or was revoked — reconnect from the source page |
| The assistant can no longer do arithmetic | A numeric column was set to `pseudonym` in AI Privacy; numbers should normally be `passthrough` |
| A unified-API search returns `400 bad_search` | The term is allow-listed to letters, digits, spaces and `. @ / + - _` |
| Fresh install, unknown admin password | It was generated and printed once in the backend log; set `EDI_DEFAULT_ADMIN_PASSWORD` to pick your own |
| Port 8000 already in use (another app / Docker container) | Run uvicorn on another port (e.g. `--port 8010`) and set `NEXT_PUBLIC_API_URL=http://localhost:8010/api` in `frontend/.env.local` |

## Unified API — attaching another application

`/api/v1/unified/*` is the contract an external application attaches to. It returns everything the key can
reach, in one shape, regardless of how many systems are behind it.

```
GET  /api/v1/unified/schema     entities, tables, columns
GET  /api/v1/unified/records    the data (filters: entity, source, table, search; format=json|ndjson)
POST /api/v1/unified/ask        a natural-language question, anonymised end to end
```

Every response — success or failure — has the same keys: `ok`, `request_id`, `data`, `page`, `sources`,
`warnings`. `sources` always reports each contributing system and when it last synced, so a consumer can
see that one of five is days stale. `warnings` reports a source that failed rather than silently returning
fewer rows.

**Keys** are created in Administration, shown **once**, and stored only as a hash. Scope one to specific
entities or sources and the limit is applied inside the query, so it cannot infer counts for data it
cannot read. Revocation takes effect on the next request.

```python
from edi_client import EDI          # client/edi_client.py — copy it anywhere, needs only httpx

edi = EDI("http://localhost:8000", api_key="edi_live_...")
for r in edi.records(entity="customer", limit=500):   # cursors handled for you
    print(r.fields, r.source.name, r.lineage["query_ref"])

a = edi.ask("which customers churned after the price change?")
print(a.text, a.anonymization)      # how many values were withheld from the model
```

Use `format=ndjson` for large exports — it streams, so neither side buffers the whole result.

## API

OpenAPI at `http://localhost:8000/api/docs`. Main groups: `/api/auth`, `/api/sources` (CRUD, test, sync,
`auth/start`, `auth/callback`, `auth/poll`), `/api/catalog`, `/api/semantic` (metrics, glossary, entities,
relationships, graph, versions, search), `/api/assistant` (ask = SSE, conversations, sql, insights),
`/api/privacy` (column policies, summary, vault purge), `/api/v1/unified` (schema, records, ask, keys) and
`/api/admin` (ai providers, usage, users, permissions, audit, queries, jobs, quality, dashboard,
monitoring).
