# Enterprise Data Intelligence Platform

A unified AI data-intelligence and enterprise data-context platform. It connects to many data sources,
builds a governed **semantic layer** (catalog, business definitions, metrics, entities, relationships,
quality), and lets an AI **investigate the whole data ecosystem** — not a single table — to produce
evidence-backed, cross-domain answers to questions such as *“Why did revenue drop in August?”*.

Two properties shape everything else:

* **Every number is traceable.** Each figure in an answer carries a `Q-xxxxxxxx` query reference you can
  click to see the SQL, the source and the rows. Citations are validated against queries that actually
  ran; anything unverifiable is flagged rather than rendered.
* **The AI never sees your real data.** Protected values are replaced with reversible tokens before a
  request leaves the machine, and the model's answer is mapped back locally. See
  [AI privacy](#ai-privacy) and [docs/security.md](docs/security.md).

```
User question → Plan → Deterministic cross-domain investigation → anonymise → LLM synthesis (tools)
                        (metric decomposition, relationship-graph traversal, related metrics,      ↓
                         correlation, data quality, freshness)                          restore → cited answer
```

## Quick start

**Windows — one click:** double-click **`start.bat`**. It installs everything on the first run (virtualenv, npm packages, demo dataset, seeded catalog), picks a free backend port, starts both servers and opens the app. Close the two server windows to stop.

Full walkthrough of how the system works: **[`docs/how-it-works.html`](docs/how-it-works.html)** — open it in any browser, no server needed.

### Manual start (any OS)

```bash
# 1. backend
python -m venv .venv && .venv/Scripts/pip install -r backend/requirements.txt      # Windows
#   (or: source .venv/bin/activate && pip install -r backend/requirements.txt)
python scripts/generate_demo_data.py          # synthetic enterprise dataset with the August scenario
python scripts/seed_demo.py --reset           # registers 7 demo sources, syncs, defines metrics/glossary/
                                              # entities, configures local Ollama if running, generates insights
.venv/Scripts/python -m uvicorn backend.main:app --port 8000

# 2. frontend (Node 20+)
cd frontend && npm install && npm run dev     # http://localhost:3000
```

**Signing in.** On a first run the admin account is created with a **randomly generated password, printed
once in the backend log**:

```
WARNING Created admin admin@example.com with a generated password: <...> This is shown ONCE.
```

To pick your own instead, set `EDI_DEFAULT_ADMIN_PASSWORD` before the first start. The seeded demo users
`analyst@example.com` and `viewer@example.com` use `demo1234`.

Then ask **“Why did revenue decline in August?”**.

Without an AI provider the assistant still answers deterministically (the investigation engine renders a
structured, cited Markdown answer). Add a provider in **Administration → AI Providers** (OpenAI, Anthropic,
Gemini, Azure OpenAI, Ollama, OpenRouter, any OpenAI-compatible endpoint) for LLM narratives and tool use.

> **Before deploying anywhere but localhost**, read [docs/security.md](docs/security.md). With
> `EDI_ENVIRONMENT=production` the app refuses to start on a default secret key, a default admin password
> or a localhost CORS origin.

## AI privacy

Values from protected columns never reach an AI provider.

Each column carries a strategy — reversible `pseudonym` tokens, `redact`, `mask`, `hash`, `generalize`
(bands) or `shift` (dates moved by a per-job constant, so intervals stay accurate). Defaults come from the
PII classifier the profiler already runs, so names, emails, phone numbers, national IDs and salary are
protected from the first sync without configuration. Numeric columns pass through by default, because
tokenising them would stop the investigation engine computing anything.

Free text is scrubbed twice: against the values already tokenised from structured columns — which is how a
customer name buried in a support note gets caught — and then with regex detectors. An **egress guard**
checks the fully serialised request body immediately before the HTTP call and aborts the request if any
original survives. The answer is mapped back to real values locally, and tokens the model invented are
reported rather than printed.

Manage it at **AI Privacy** in the UI: every column, its detected strategy, and a live preview of exactly
what the model would receive. `docs/security.md` documents the limits as plainly as the protections.

## Unified API

One contract any external application can attach to, regardless of how many systems are behind it.

```
GET  /api/v1/unified/schema      entities, tables and columns this key can reach
GET  /api/v1/unified/records     the data — one envelope, lineage and source freshness on every record
POST /api/v1/unified/ask         a natural-language question, anonymised end to end
```

```python
from edi_client import EDI                      # client/edi_client.py — one file, needs only httpx

edi = EDI("http://localhost:8000", api_key="edi_live_...")
for record in edi.records(entity="customer"):   # follows cursors for you
    print(record.fields, record.source.name, record.lineage)

answer = edi.ask("why did revenue decline in August?")
print(answer.text, answer.anonymization)        # proof the model saw tokens, not real values
```

API keys are created in **Administration**, hashed with SHA-256, shown once, and scoped by entity and
source — scoping is applied *inside* the query, so a restricted key cannot infer counts it may not read.
Responses use one envelope for successes and errors alike, with `warnings` for partial failures so a
broken source is reported rather than silently shrinking the result.

## Docker Compose

```bash
cp .env.example .env
docker compose up --build            # postgres (pgvector), redis, backend, worker, frontend
docker compose exec backend python scripts/generate_demo_data.py
docker compose exec backend python scripts/seed_demo.py
```

Host ports are configurable: `EDI_PG_PORT`, `EDI_REDIS_PORT`.

## What is inside

| Layer | Where | Highlights |
|---|---|---|
| Connectors | `backend/connectors/` | Plugin registry, **17 source types**: PostgreSQL, MySQL/MariaDB, SQL Server, Oracle, SQLite, DuckDB, files (CSV/TSV/Excel/JSON/JSONL/Parquet/XML), Redis, MongoDB, REST, GraphQL, **Google Sheets**, **BigQuery**, **Excel on OneDrive/SharePoint**, **SharePoint lists**, **Power BI semantic models**, **Tableau Server/Cloud**. Non-SQL sources are materialised into DuckDB so everything is SQL-queryable. |
| Authentication | `backend/auth_providers/` | Five modes behind one interface — **service account**, **browser OAuth** (authorization code + PKCE), **device code**, **stored credentials**, **ambient/managed identity**. A connector declares which it accepts and implements none of them; credentials are encrypted at rest and refreshed transparently under a lock. |
| AI privacy | `backend/anonymize/` | Per-column strategies, a job-scoped reversible token vault, free-text scrubbing and an unconditional egress guard. Tokens are stable within one answer and different in the next, so they never become a durable identifier. |
| BI semantic models | `backend/connectors/{powerbi,tableau}_connector.py`, `backend/semantic/{expressions,bi_import}.py` | Imports the governed model a BI platform already owns — measures (DAX / Tableau formulas), declared relationships, field descriptions. Formulas are translated to SQL by a real parser; anything not translatable exactly becomes a **definition-only** metric that keeps its native formula and is never given a computed value. |
| Metadata catalog | `backend/metadata/` | Sources → datasets → tables → columns; profiling (nulls, distinct, min/max/mean/median/std, percentiles, outliers, frequency, date range); PII detection; heuristic + AI-suggested descriptions. |
| Semantic layer | `backend/semantic/`, `backend/knowledge_graph/`, `backend/vector_store/` | Canonical entities & cross-system entity resolution, relationship discovery (FKs, names, types, cardinality, value overlap) with approve/reject, metrics catalog, glossary + business rules, versioning, hybrid lexical/vector search. |
| Query engine | `backend/query_engine/` | Strict read-only SQL validation, per-dialect SQL generation, permission-aware executor (dataset/table/column rules, row-level filters, PII masking), query log with `Q-xxxxxxxx` refs. |
| Analytics | `backend/analytics/` | Deterministic investigation engine (decomposition + contribution, multi-hop related metrics, driver re-segmentation, lag detection, correlation, quality & freshness), insight/anomaly engine. |
| AI | `backend/ai/` | Provider abstraction (OpenAI-compatible + Anthropic), planner (intent/metric/period/follow-ups), tool-calling orchestrator with streaming, cost tracking. Python analysis is **off by default** — see `docs/security.md`. |
| Unified API | `backend/api/routers/unified.py`, `client/` | One envelope, scoped API keys, opaque cursors, ndjson streaming, embeddable Python client. |
| Security & audit | `backend/security/`, `backend/audit/`, `backend/workers/retention.py` | JWT auth, RBAC (admin/analyst/viewer), audit log, encrypted credentials at rest, data-retention sweeps for stored result rows and the token vault. |
| Frontend | `frontend/` | Next.js + TypeScript + Tailwind + ECharts: dashboard, sources, datasets explorer, semantic layer, glossary, metrics, relationship graph, data quality, **AI privacy**, AI assistant (streaming, evidence, SQL, charts, follow-ups), insights, query history, audit, administration. |

## Tests

```bash
.venv/Scripts/python -m pytest                     # 204 tests: unit, integration, security
EDI_TEST_PG_URL=postgresql://edi:edi@localhost:5442/edi EDI_TEST_REDIS_URL=redis://localhost:6389/0 pytest   # + Postgres/Redis
EDI_TEST_LLM=1 pytest tests/ai                     # + LLM narrative checks (needs local Ollama or a configured provider)
pytest tests/security                              # the attack surface: every case asserts a refusal
```

`tests/security/` is deliberately adversarial — it asserts that injection payloads are rejected, that the
egress guard fires, that secrets are never echoed, and that production refuses insecure defaults.

## Documentation

Want to see the Power BI / Tableau import without a real tenant? Run `python scripts/bi_demo_stubs.py`
and follow the printed connection settings.

Start with **[docs/how-it-works.html](docs/how-it-works.html)** (visual explainer). Also in [docs/](docs/):

* **[security.md](docs/security.md)** — deployment checklist, the AI privacy boundary **and its limits**, retention
* **[guide.md](docs/guide.md)** — user & operator guide: sources, auth modes, semantic layer, privacy, unified API
* **[architecture.md](docs/architecture.md)** — how the pieces fit together

API reference at `http://localhost:8000/api/docs`.
