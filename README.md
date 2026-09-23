# Enterprise Data Intelligence Platform

A unified AI data-intelligence and enterprise data-context platform. It connects to many data sources,
builds a governed **semantic layer** (catalog, business definitions, metrics, entities, relationships,
quality), and lets an AI **investigate the whole data ecosystem** — not a single table — to produce
evidence-backed, cross-domain answers to questions such as *“Why did revenue drop in August?”*.

```
User question → Plan → Deterministic cross-domain investigation → LLM synthesis (tools) → Evidence-cited answer
                        (metric decomposition, relationship-graph traversal, related metrics,
                         correlation, data quality, freshness — every number has a query ref)
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
python scripts/seed_demo.py --reset           # registers 7 sources, syncs, defines metrics/glossary/entities,
                                              # configures local Ollama if running, generates insights
.venv/Scripts/python -m uvicorn backend.main:app --port 8000

# 2. frontend (Node 20+)
cd frontend && npm install && npm run dev     # http://localhost:3000
```

Sign in with `admin@example.com / admin123` (also `analyst@example.com` / `viewer@example.com`, password `demo1234`)
and ask **“Why did revenue decline in August?”**.

Without an AI provider the assistant still answers deterministically (the investigation engine renders a
structured, cited Markdown answer). Add a provider in **Administration → AI Providers** (OpenAI, Anthropic,
Gemini, Azure OpenAI, Ollama, OpenRouter, any OpenAI-compatible endpoint) for LLM narratives and tool use.

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
| Connectors | `backend/connectors/` | Plugin registry; PostgreSQL, MySQL/MariaDB, SQL Server, Oracle, SQLite, DuckDB, files (CSV/TSV/Excel/JSON/JSONL/Parquet/XML), Redis, MongoDB, REST, GraphQL, **Power BI semantic models** and **Tableau Server/Cloud**. Non-SQL sources are materialised into DuckDB so everything is SQL-queryable. |
| BI semantic models | `backend/connectors/{powerbi,tableau}_connector.py`, `backend/semantic/{expressions,bi_import}.py` | Imports the governed model a BI platform already owns — measures (DAX / Tableau formulas), declared relationships, field descriptions. Formulas are translated to SQL by a real parser; anything not translatable exactly becomes a **definition-only** metric that keeps its native formula and is never given a computed value. |
| Metadata catalog | `backend/metadata/` | Sources → datasets → tables → columns; profiling (nulls, distinct, min/max/mean/median/std, percentiles, outliers, frequency, date range); PII detection; heuristic + AI-suggested descriptions. |
| Semantic layer | `backend/semantic/`, `backend/knowledge_graph/`, `backend/vector_store/` | Canonical entities & cross-system entity resolution, relationship discovery (FKs, names, types, cardinality, value overlap) with approve/reject, metrics catalog, glossary + business rules, versioning, hybrid lexical/vector search. |
| Query engine | `backend/query_engine/` | Strict read-only SQL validation, per-dialect SQL generation, permission-aware executor (dataset/table/column rules, row-level filters, PII masking), query log with `Q-xxxxxxxx` refs. |
| Analytics | `backend/analytics/` | Deterministic investigation engine (decomposition + contribution, multi-hop related metrics, driver re-segmentation, lag detection, correlation, quality & freshness), insight/anomaly engine. |
| AI | `backend/ai/` | Provider abstraction (OpenAI-compatible + Anthropic), planner (intent/metric/period/follow-ups), tool-calling orchestrator with streaming, sandboxed Python analysis, cost tracking. |
| Security & audit | `backend/security/`, `backend/audit/` | JWT auth, RBAC (admin/analyst/viewer), audit log, encrypted credentials at rest. |
| Frontend | `frontend/` | Next.js + TypeScript + Tailwind + ECharts: dashboard, sources, datasets explorer, semantic layer, glossary, metrics, relationship graph, data quality, AI assistant (streaming, evidence, SQL, charts, follow-ups), insights, query history, audit, administration. |

## Tests

```bash
.venv/Scripts/python -m pytest                     # unit + integration (SQLite/CSV/JSON/Excel/REST) + AI benchmark
EDI_TEST_PG_URL=postgresql://edi:edi@localhost:5442/edi EDI_TEST_REDIS_URL=redis://localhost:6389/0 pytest   # + Postgres/Redis
EDI_TEST_LLM=1 pytest tests/ai                     # + LLM narrative checks (needs local Ollama or a configured provider)
```

## Documentation

Want to see the Power BI / Tableau import without a real tenant? Run `python scripts/bi_demo_stubs.py`
and follow the printed connection settings.

Start with **[docs/how-it-works.html](docs/how-it-works.html)** (visual explainer). Also in [docs/](docs/): architecture, installation, configuration, data sources & connectors, semantic layer,
AI providers, query engine, security, adding connectors/metrics/business rules, troubleshooting, API reference
(`http://localhost:8000/api/docs`).
