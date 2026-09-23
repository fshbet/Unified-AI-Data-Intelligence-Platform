# Documentation

| Document | Contents |
|---|---|
| [how-it-works.html](how-it-works.html) | **Start here.** Self-contained visual explainer: the core idea, running the app, the seven layers, a full worked example of the demo question, security, screens, project layout, troubleshooting. Opens offline in any browser. |
| [architecture.md](architecture.md) | Layers, request flow for a diagnostic question, storage, background work, extensibility |
| [guide.md](guide.md) | Installation, configuration, data sources & connectors, semantic layer, glossary & metrics, relationships, data quality, AI assistant, insights, administration, security, adding connectors/metrics/business rules, troubleshooting, API |
| `http://localhost:8000/api/docs` | Live OpenAPI reference (Swagger UI) |

Quick reference for developers:

* `backend/connectors/base.py` — the `DataConnector` interface every connector implements.
* `backend/metadata/models.py` — the metadata catalog / semantic layer schema (Alembic migrations in `migrations/`).
* `backend/analytics/investigation.py` — the deterministic cross-domain investigation engine.
* `backend/ai/orchestrator.py`, `backend/ai/tools.py`, `backend/ai/planner.py` — the AI reasoning layer.
* `backend/query_engine/` — SQL safety, dialects, permission-aware execution.
* `tests/` — unit, integration (SQLite/CSV/Excel/JSON/REST/Postgres/Redis) and AI benchmark tests.
