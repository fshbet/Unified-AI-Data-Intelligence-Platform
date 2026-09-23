# Architecture

## Principle

The AI never “looks at a database”. It reasons over an **organisational semantic layer** and investigates
the whole ecosystem through governed metrics, approved relationships and permission-checked tools.

```
Frontend (Next.js) ──HTTP/SSE──▶ FastAPI (backend/api)
                                     │
        ┌────────────────────────────┼─────────────────────────────┐
        ▼                            ▼                             ▼
 Connector service            Metadata / Semantic            AI orchestrator
 backend/connectors           backend/metadata               backend/ai
 (plugin registry)            backend/semantic               planner → investigation → tool loop → stream
        │                     backend/knowledge_graph              │
        │                     backend/vector_store                 │
        ▼                            ▼                             ▼
 Source systems               Metadata catalog (SQLAlchemy)   Query engine  backend/query_engine
 (SQL, files→DuckDB,          SQLite (dev) / PostgreSQL       safety → permissions/RLS/masking → dialect SQL → log
  Redis, Mongo, REST)                                          Analytics   backend/analytics
                                                               Data quality backend/data_quality
                                                               Security / Audit
                                                               Workers (sync, insights, scheduler)
```

## Request flow: “Why did revenue drop in August?”

1. **Planner** (`ai/planner.py`) — intent (`diagnostic`), governed metric (`revenue` via catalog/glossary
   synonyms), period (`August 2026` vs `July 2026`), dimensions, domains, candidate tables (catalog search),
   follow-up resolution from conversation context, optional LLM refinement when the catalog match is ambiguous.
   If no period is stated but the question mentions a decline/increase, the most recent month with a ≥5%
   move in that direction is chosen.
2. **Investigation engine** (`analytics/investigation.py`) — deterministic, parallel:
   validate headline change → 12-month trend → decomposition by every dimension with contribution % →
   detect *disproportionate* drivers (e.g. `region=West`, `product_id=P003`) → traverse the knowledge graph
   (approved relationships + entity mappings, up to 3 hops) → compute every reachable governed metric for both
   periods, re-segmented by the drivers and their intersection → lag detection (prior-month shocks) →
   monthly correlation (labelled *correlation only*) → open data-quality issues → source freshness.
   Every number carries a `Q-` query reference.
3. **LLM synthesis** (`ai/orchestrator.py`) — the model receives the plan and the compact investigation (never
   raw tables), may call tools (`search_metadata`, `get_schema`, `execute_sql`, `compare_periods`,
   `detect_anomalies`, `execute_python_analysis`, …) to fill gaps, then streams a structured Markdown answer
   citing query refs. Repeated tool calls are de-duplicated; the loop is capped when the investigation is rich.
4. **Answer payload** — plan, investigation, evidence, queries (SQL + preview), sources, tables, charts,
   reasoning path, confidence (evidence strength, quality issues, staleness, warnings), usage/cost, follow-ups.
   Without an LLM the same payload is rendered by a deterministic template.

## Storage

* **Metadata catalog** — SQLAlchemy models in `backend/metadata/models.py` (Alembic migrations in
  `migrations/`). SQLite by default; PostgreSQL in Docker.
* **Materialised sources** — one DuckDB file per non-SQL source under `data/duckdb/`.
* **Vector store** — `vector_store/store.py` (`CatalogVectorStore`): embeddings in the catalog DB with
  numpy cosine + BM25 hybrid search. Implement the `VectorStore` protocol for pgvector/Qdrant.
* **Uploads** — `data/uploads/<source_id>/`.

## Background work

`workers/jobs.py`: thread-pool jobs (`sync_source`, `generate_insights`, `reindex`, `ai_describe`) recorded in
the `jobs` table; a scheduler thread honours each source's refresh frequency (15m/hourly/daily/weekly). In
Docker the `worker` service runs the scheduler loop.

## Extensibility

* New connector: subclass `DataConnector` (or `MaterializedConnector`) and decorate with `@register`.
* New AI provider: implement `AIProvider` (`chat`, `stream`, `generate_embedding`) or reuse the OpenAI-compatible class.
* New LLM tool: add a spec to `TOOL_SPECS` and a `t_<name>` method on `ToolBox`.
* New quality rule: extend `data_quality/engine.py::run_quality_checks`.
