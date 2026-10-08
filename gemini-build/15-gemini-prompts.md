# 15 — Prompts for Gemini

Copy-paste prompts, one per phase. The pattern is always the same:

> **New Gemini session** → paste `01-conventions.md` → paste the phase file → paste the prompt
> below → build → run the gate.

Do not combine phases. Do not continue a phase in the session that built the previous one.

---

## The opening prompt (use for every phase)

```
You are helping me build a Python application in small, independently validated phases.

I have given you two documents:
1. CONVENTIONS — rules that apply to every phase. Follow them exactly.
2. PHASE SPEC — the phase we are building now.

Build ONLY what the phase spec lists. Do not implement future phases. Do not add features,
abstractions, configuration options or dependencies that the spec does not ask for.

Output every file in full, each with its path as a heading. No placeholders, no "..." and no
"implement this later" comments — I am going to run this code as-is.

When you are done, stop and list the exact commands from the Validation Gate so I can run them.

If anything in the spec is ambiguous or looks wrong, say so before writing code.
```

---

## Phase 00 — Foundations

```
Build Phase 00. I need a runnable FastAPI app with SQLite, a migration runner, config,
structured logging with secret redaction, and the error hierarchy.

Constraints worth repeating:
- SQLite only. No Alembic — numbered .sql files and a small runner.
- Every PRAGMA from the conventions, applied per connection, one connection per thread.
- A migration file that fails must roll back entirely.

Also write tests/unit/test_foundations.py covering the five cases in the gate.
```

## Phase 01 — Plugin framework

```
Build Phase 01: the plugin registry and the five base classes.

Critical detail — autoload() must guard on a module-level _LOADED flag, NOT on the registry
being non-empty. Something else will import a single plugin first, and a "skip if populated"
check then silently hides every other plugin while all unit tests still pass. Write the
regression test for this explicitly.

A plugin whose optional dependency is missing must register as unavailable with an install
hint, never crash the app at import time.
```

## Phase 02 — Auth & secrets

```
Build Phase 02: the encrypted vault and all five auth modes (service_account, oauth_code,
device_code, credentials, ambient), plus the connections API.

Non-negotiable:
- Fernet, key derived via HKDF from secret_key. Secrets only ever stored as ciphertext.
- Credential.__repr__ must never print the secret.
- PKCE and single-use state verification on the OAuth code flow.
- get_credential() refreshes transparently under a lock so N parallel callers cause one refresh.
- Token caches are per connection instance, never class attributes.

Include tests/stubs/oauth_stub.py using http.server — do not mock the HTTP client.
```

## Phase 03 — SQL connectors

```
Build Phase 03: the read-only SQL safety validator, the dialect table, and one SQL connector
covering sqlite, postgresql, mysql and mssql.

The validator must strip comments BEFORE splitting on semicolons, and must match blocked
keywords as whole words so a column named created_at is not rejected. Write the full accept/
reject test table from the gate.

Pagination must be keyset-based, not OFFSET. Quote every identifier.
```

## Phase 04 — Google connectors

```
Build Phase 04: Google Sheets, BigQuery and Drive connectors supporting service_account,
oauth_code and ambient auth.

Handle the real-world Sheets mess: blank header cells, duplicate headers, headers with
trailing spaces, mixed-type columns, tab names with spaces and parentheses. Use
valueRenderOption=UNFORMATTED_VALUE.

BigQuery must always set maximumBytesBilled and must route queries through the Phase 03
read-only validator.

If a service-account request 404s, raise an error naming the SA email and telling the user to
share the file with it.

Include tests/stubs/google_stub.py speaking the real protocol over http.server.
```

## Phase 05 — Microsoft connectors

```
Build Phase 05: Microsoft Graph (Excel/Outlook), SharePoint lists, Azure SQL and Dataverse,
supporting service_account (client credentials), oauth_code, device_code and credentials (ROPC,
clearly discouraged in the UI).

Specifics:
- Application vs delegated permissions. Detect AADSTS65001 and report it as missing admin
  consent, naming the permission.
- Follow @odata.nextLink verbatim; never rebuild it.
- Honour Retry-After on 429.
- offline_access in delegated scopes or there is no refresh token.
- azure_sql must subclass the Phase 03 connector, not reimplement SQL handling.
```

## Phase 06 — Unified store

```
Build Phase 06: the canonical record store, ingestion, schema mapping, sync and the unified
query.

Key requirements:
- content_hash over canonicalised (sorted-key) JSON so unchanged rows are not rewritten.
- Upsert, never delete-then-insert.
- Full sync soft-deletes missing records; incremental sync must NEVER mark anything deleted.
- The unified query takes filters, FTS search and keyset cursors, and validates filter names
  against known columns before building SQL. This endpoint becomes public, so treat it as the
  main injection surface.
- QueryResult reports which sources contributed and when each last synced.
```

## Phase 07 — Anonymisation  ← the one to get right

```
Build Phase 07: the anonymisation layer.

I am giving you a WORKING reference implementation (reference/anonymizer.py) whose self-check
passes. Use its algorithm. Your job is to adapt it into the app: policies in SQLite, the vault
encrypted at rest with the Phase 02 Fernet key, job-scoped salts, and the policy API.

Do not change these properties:
- Deterministic tokens within a job, different tokens across jobs.
- The dictionary pass over free text, sorted LONGEST FIRST.
- re.escape on every original value.
- assert_no_leak runs on the serialised payload and raises LeakError naming the leaked values.
- deanonymize returns unresolved tokens rather than silently leaving them.
- Numeric columns default to passthrough so the model can still do arithmetic.

Port every assertion from the reference demo() into tests/unit/test_anonymize.py, plus the
500-row fuzz test with quotes, accents, emoji and regex metacharacters.
```

## Phase 08 — AI gateway

```
Build Phase 08: the AI gateway and provider plugins for Gemini, OpenAI-compatible (covering
OpenAI, Azure OpenAI and Ollama) and Anthropic.

The security structure matters more than the features:
- gateway.py is the ONLY module that may import an AI provider.
- Provider plugins receive messages and return text. They never receive the vault, the policies
  or a database connection.
- assert_no_leak lives in the provider BASE CLASS, on the serialised request body, immediately
  before the HTTP call. A provider added later must inherit it without thinking about it.
- complete() called without a vault must raise LeakError — fail closed on the plumbing.
- Anonymise the user's question too.

tests/stubs/ai_stub.py must RECORD every request body, so the tests can assert on exactly what
would have gone over the wire.
```

## Phase 09 — Unified API

```
Build Phase 09: the unified output API, API keys, and the embeddable Python client.

- Four endpoints only: schema, records, ask, openapi.json.
- One envelope for every response including errors, always carrying `sources` with last_sync_at
  and a stale flag, and `warnings` for partial failures.
- API keys hashed with sha256, shown once, scoped by entity and source — and that scoping must
  be applied inside the query so page.total cannot leak counts the caller may not see.
- Opaque base64 cursors, never offsets.
- format=ndjson must stream with flat memory.
- udal_client.records() returns a generator that follows cursors automatically.
```

## Phase 10 — Frontend

```
Build Phase 10: the UI with FastAPI + Jinja2 + HTMX. No Node, no build step, htmx.min.js
vendored locally so the app works offline.

The connection form must be generated entirely from manifest.config_fields plus the auth
plugin's fields. There must be no per-connector template branch — I will grep for connector
names in the templates.

The /privacy page is the important one: every column, its detected strategy pre-selected but
NOT auto-applied, and a live Preview showing exactly what the AI would see for a masked sample
value.

Note: the SSE stream sends \r\n, so normalise line endings client-side before splitting on
blank lines, or the stream will silently appear empty.
```

## Phase 11 — Packaging

```
Build Phase 11: the CLI (init/serve/sync/key/doctor), packaging, the embedding examples, and
the docs.

udal doctor must include an anonymisation self-test that runs a known row through
anonymise -> guard -> de-anonymise, and must state plainly that the install is unsafe if it
fails.

Everything must work when mounted under a path prefix: root_path-aware URLs, OAuth redirect
URIs built from the request, relative static paths.

docs/SECURITY.md must state the limits of the anonymisation honestly, not just its strengths.
```

---

## When a gate fails

```
The gate failed. Here is the exact command and the full output:

[paste command]
[paste output]

Fix the root cause rather than the symptom. If the fix belongs in a different file from the one
that threw, change that file. Show me only the files that change, in full.
```

## When Gemini over-builds

```
That is more than the spec asked for. Remove [X]. The phase spec is the scope — I do not want
abstractions, configuration or dependencies for requirements I have not stated. Give me the
smallest version that passes the gate.
```

## When Gemini substitutes technology

```
No. This project is Python + SQLite only, by requirement:
- No Postgres, Redis, Celery or message broker — ThreadPoolExecutor and a jobs table.
- No React/Next/Node — Jinja2 and HTMX, server-rendered.
- No ORM — plain parameterised SQL.
Rewrite it that way.
```

---

## A note on context

If a phase is too large for one response, ask for it in parts **by file**, not by "part 1 of 3":

```
Give me udal/anonymize/vault.py and udal/anonymize/engine.py in full first. I will ask for the
rest after I have run them.
```

Files that run are progress. A half-written file is not.
