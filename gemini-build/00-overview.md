# 00 — Architecture Overview

Read this once before Phase 00. It is the map; the phase files are the territory.

---

## The product in one paragraph

A self-hosted application that connects to an organisation's scattered data systems — Google
Sheets and BigQuery, Microsoft 365 and Azure SQL, Postgres/MySQL/SQL Server, files and REST
APIs — through pluggable connectors, each of which can authenticate several different ways. It
pulls those systems into one normalised local store backed by SQLite, exposes **one** API that
any other application can attach to, and lets an AI answer questions over that data without the
real values ever leaving the machine.

---

## The layer map

```
                        ┌─────────────────────────────────────────────┐
  any application ────► │  UNIFIED OUTPUT API      /v1/unified/*      │  Phase 09
                        │  one contract, one envelope, OpenAPI        │
                        └───────────────┬─────────────────────────────┘
                                        │
      browser ─────────────────────────►│  server-rendered UI          Phase 10
                                        │
                        ┌───────────────▼─────────────────────────────┐
                        │  AI GATEWAY                                 │  Phase 08
                        │   anonymise → send → restore                │
                        │   ┌───────────────────────────────────┐     │
                        │   │ ANONYMISATION LAYER               │     │  Phase 07
                        │   │  policies · vault · EGRESS GUARD  │     │
                        │   └───────────────────────────────────┘     │
                        └───────────────┬─────────────────────────────┘
                                        │ only tokenised data crosses this line
                        ┌───────────────▼─────────────────────────────┐
                        │  UNIFIED STORE   canonical records + lineage│  Phase 06
                        └───────────────┬─────────────────────────────┘
                                        │
                        ┌───────────────▼─────────────────────────────┐
                        │  CONNECTOR PLUGINS                          │  Phases 03-05
                        │   sql · google · microsoft · file · rest    │
                        └───────────────┬─────────────────────────────┘
                                        │
                        ┌───────────────▼─────────────────────────────┐
                        │  AUTH PLUGINS                               │  Phase 02
                        │   service account · oauth · device · creds  │
                        │   encrypted secret vault                    │
                        └───────────────┬─────────────────────────────┘
                                        │
                        ┌───────────────▼─────────────────────────────┐
                        │  PLUGIN REGISTRY · CONFIG · SQLITE          │  Phases 00-01
                        └─────────────────────────────────────────────┘
```

Two lines in that diagram are load-bearing and everything else serves them:

- **The egress line.** Nothing below it reaches an AI provider in its original form. Phase 07
  builds the guard that enforces this, and Phase 08 wires it in so there is no bypass.
- **The unified store line.** Everything above it works on one record shape, so adding a
  twelfth connector changes nothing upstream.

---

## Data flow: one question, end to end

What happens when someone asks *"which customers churned after the price change?"*:

```
1  UI/API        question arrives at /v1/unified/ask
2  planner       decides which canonical entities are relevant        (no AI yet)
3  store         SELECTs the rows from SQLite                         (real values)
4  policy        looks up the anonymisation policy per column
5  anonymiser    name -> [[PER_0001]], email -> [[EML_0001]],
                 dates shifted by a per-job constant, salary bucketed
6  EGRESS GUARD  asserts no original value survives in the payload    <-- refuses to send
7  AI provider   Gemini/OpenAI/Anthropic/Ollama answers using tokens only
8  de-anonymise  [[PER_0001]] -> "Priya Raman" in the answer
9  audit         records what was asked, what was sent, what came back
10 response      answer + the rows it was based on + lineage
```

Step 6 is unconditional. It runs on the fully serialised request body, immediately before the
HTTP call — not earlier — so it also covers the system prompt, few-shot examples, and any field
a future refactor forgets to route through the anonymiser.

---

## The canonical record

Every connector, no matter how exotic, produces this shape. It is the reason the app can offer
one output contract.

```python
@dataclass(frozen=True)
class Record:
    source_id:   str              # which connection this came from
    source_type: str              # "google_sheets", "azure_sql", ...
    entity:      str              # canonical entity: "customer", "invoice", "employee"
    natural_key: str              # stable id within the source
    fields:      dict[str, Any]   # the data
    fetched_at:  datetime
    lineage:     dict[str, Any]   # table/sheet/url, row number, sync id, query used
```

`lineage` is not decoration. It is how an answer can be traced back to the system it came from,
and it is what lets you tell a user *"this number came from the Finance workspace sheet, tab
Q3, row 412, synced 40 minutes ago"*.

### Entities

A small fixed vocabulary that connectors map onto: `customer`, `contact`, `employee`, `product`,
`order`, `invoice`, `payment`, `ticket`, `event`, `document`, `metric`, `other`. Keep it small.
A connector that cannot map cleanly uses `other` and sets `lineage["raw_type"]`.

---

## Technology choices

| Concern | Choice | Why not the obvious alternative |
|---|---|---|
| Web framework | FastAPI + Uvicorn | Gives OpenAPI free, which *is* the Phase 09 deliverable |
| Database | SQLite, WAL mode | Requirement. Handles this workload to millions of rows |
| Migrations | Numbered `.sql` files + 20-line runner | Alembic is a dependency and a mental model for 6 tables |
| Background work | `ThreadPoolExecutor` + a `jobs` table | Celery needs a broker; syncs are I/O-bound, threads are fine |
| Templates | Jinja2 | Comes with FastAPI's ecosystem; keeps the app pure Python |
| Frontend interactivity | HTMX, vendored | ~14 KB, no build step, no `node_modules` |
| Secrets | `cryptography` Fernet | Do not hand-roll. This is the one place to spend a dependency |
| HTTP | `httpx` | Sync and async, used by every connector |

### Dependency policy

Core install stays small:

```
fastapi  uvicorn  pydantic  pydantic-settings  jinja2  httpx  cryptography  python-multipart
```

Everything a connector needs is an **optional extra**, so a user who only reads SQLite never
installs Google's SDK:

```
pip install -e ".[google]"      # google-auth, google-auth-oauthlib, google-api-python-client
pip install -e ".[microsoft]"   # msal
pip install -e ".[postgres]"    # psycopg[binary]
pip install -e ".[all]"
```

A connector whose extra is missing must still *appear* in the registry, marked unavailable with
an install hint. It must not crash the app at import time. Phase 01's gate tests this.

---

## Repository layout

```
udal/
├── core/
│   ├── config.py            settings from env, prefix UDAL_
│   ├── db.py                connection factory, WAL, row factory
│   ├── migrations.py        the runner
│   ├── errors.py            the exception hierarchy
│   └── logging.py           structured logs, secret redaction
├── migrations/
│   ├── 001_initial.sql
│   ├── 002_auth.sql
│   └── ...
├── plugins/
│   ├── registry.py          discovery, manifests, lifecycle
│   ├── base.py              the five plugin ABCs
│   ├── auth/                service_account, oauth_code, device_code, credentials, ambient
│   ├── connectors/          sql, google_sheets, bigquery, ms_graph, azure_sql, file, rest
│   ├── anonymizers/         default strategies, custom detectors
│   ├── ai/                  gemini, openai, anthropic, ollama
│   └── sinks/               json, csv, webhook
├── store/
│   ├── models.py            Record and friends
│   ├── ingest.py            connector output -> canonical rows
│   └── query.py             the unified query
├── anonymize/
│   ├── policy.py            per-column rules
│   ├── vault.py             token store
│   ├── engine.py            apply / scrub / restore
│   └── guard.py             assert_no_leak
├── ai/
│   ├── gateway.py           the pipeline
│   └── prompts.py
├── api/
│   ├── app.py               FastAPI app factory
│   └── routers/             unified, sources, policies, admin, ui
├── web/
│   ├── templates/
│   └── static/
├── client/
│   └── udal_client.py       the embeddable Python client
├── tests/
│   ├── unit/
│   ├── integration/
│   └── stubs/               fake Google/Microsoft/SQL servers
├── pyproject.toml
└── run.py
```

Package name `udal` (Unified Data Access Layer). Rename if you like, but do it in Phase 00 —
not later.

---

## What "done" looks like

When all twelve gates pass, this is true:

- `pip install -e .` then `python run.py` gives a working app on a clean machine.
- A new connector is one file in `plugins/connectors/` and appears in the UI with a generated
  config form. No core file changes.
- Every connection can authenticate at least two different ways.
- One `GET /v1/unified/records` returns data from every connected system in one shape.
- Asking the AI a question sends tokens only — provable from the captured outbound request in
  the test suite — and the answer comes back with real values restored.
- Turning on anonymisation for a column takes one click and needs no code.
