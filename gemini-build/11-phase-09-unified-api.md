# Phase 09 — The Unified Output API

**Goal:** one contract that any external application can attach to and receive everything —
regardless of how many systems are behind it, how they authenticate, or what shape their data
started in.

This is the deliverable the whole app exists to produce.

**Depends on:** Phases 00–02, 06; 07–08 for the `/ask` endpoint.

---

## Build these files

```
udal/api/routers/unified.py     the public surface
udal/api/keys.py                API key issue / verify / scope
udal/api/envelope.py            the one response shape
udal/plugins/sinks/json_sink.py
udal/plugins/sinks/csv_sink.py
udal/plugins/sinks/webhook_sink.py
udal/client/udal_client.py      embeddable Python client
udal/migrations/006_api.sql
tests/integration/test_unified_api.py
```

---

## The contract

Four endpoints. Adding a fifth should feel like a failure.

```
GET  /v1/unified/schema      what entities and fields exist, and where they came from
GET  /v1/unified/records     the data
POST /v1/unified/ask         a natural-language question (anonymised end to end)
GET  /v1/unified/openapi.json   machine-readable spec for the three above
```

### One envelope, every response

```json
{
  "ok": true,
  "request_id": "req_01HXYZ",
  "data": {
    "records": [
      {
        "id": "a3f9...",
        "entity": "customer",
        "natural_key": "C-1042",
        "fields": {"name": "Priya Raman", "region": "West", "revenue": 48200},
        "source": {"connection_id": "conn_7", "type": "google_sheets", "name": "CRM Export"},
        "lineage": {"sheet_name": "Q3", "row": 412, "sync_id": "sync_88"},
        "fetched_at": "2026-10-08T09:14:02Z"
      }
    ]
  },
  "page": {"limit": 100, "returned": 1, "next_cursor": "eyJpZCI6...", "total": 4812},
  "sources": [
    {"connection_id": "conn_7", "type": "google_sheets", "name": "CRM Export",
     "last_sync_at": "2026-10-08T08:40:00Z", "stale": false, "record_count": 4812}
  ],
  "warnings": []
}
```

Rules that make this worth calling a contract:

- **Every response has this shape**, including errors (`ok: false`, `error` object, `data: null`).
  A consumer writes one parser.
- **`sources` is always present**, with `last_sync_at` and a `stale` flag. A consumer must be
  able to see that one of five systems is four days behind — without that, "all the data in one
  place" is a liability.
- **`lineage` is always present.** Any row can be traced back to its origin.
- `warnings` is for non-fatal conditions: truncation, a source that failed to sync, a partial
  result. Never silently drop a source — report it here.

### `GET /v1/unified/records`

```
?entity=customer           &source=conn_7        &search=renewal
&filter.region=West        &filter.revenue__gt=1000
&fields=name,region,revenue
&order_by=-revenue         &limit=100            &cursor=...
&format=json|ndjson|csv
```

`ndjson` streams, for consumers pulling everything. Cursors are opaque base64 — never an offset
integer, or a consumer will construct one by hand and get inconsistent pages.

### `POST /v1/unified/ask`

```json
{"question": "which customers churned after the price change?", "scope": {"entity": "customer"}}
```

```json
{
  "ok": true,
  "data": {
    "answer": "Three accounts churned within 30 days ...",
    "evidence": [ /* the real records the answer is based on */ ],
    "unresolved_tokens": [],
    "anonymization": {"job_id": "job_...", "columns_protected": 6, "rows_sent": 180, "truncated": false}
  },
  "sources": [...]
}
```

The `anonymization` block is part of the contract, not a debug field. A consumer — and an
auditor — must be able to see that protection was applied and how much.

---

## Authentication for consumers

API keys, because the consumer is a program.

```sql
CREATE TABLE IF NOT EXISTS api_keys (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    key_hash    TEXT NOT NULL UNIQUE,     -- sha256; the key itself is never stored
    prefix      TEXT NOT NULL,            -- "udal_live_a3f9" for display
    scopes      TEXT NOT NULL DEFAULT '["read"]',   -- read | ask | admin
    entities    TEXT,                     -- JSON allowlist; NULL = all
    sources     TEXT,                     -- JSON allowlist; NULL = all
    rate_limit  INTEGER NOT NULL DEFAULT 600,
    expires_at  TEXT,
    last_used_at TEXT,
    revoked_at  TEXT,
    created_at  TEXT NOT NULL
);
```

- Shown **once** at creation, stored as a sha256 hash. Never recoverable.
- `Authorization: Bearer udal_live_...`
- `entities`/`sources` allowlists mean an integration can be given exactly one entity from one
  system. Apply them inside `store.query`, never as a post-filter — a post-filter leaks `total`
  and leaks rows on any path that forgets it.
- Rate limit per key, returning `429` with `Retry-After`.
- Every call lands in `audit_log` with the key id, not the key.

CORS: enabled for `/v1/unified/*` only, origins from config. The UI and admin routes stay
same-origin.

---

## The embeddable client

Point of this phase: attaching should take three lines.

**Python** (`udal/client/udal_client.py`, no dependency beyond `httpx`):

```python
from udal_client import Udal

udal = Udal("http://localhost:8000", api_key="udal_live_...")

for record in udal.records(entity="customer", region="West"):   # auto-pages
    print(record.fields["name"], record.source.type)

answer = udal.ask("which customers churned after the price change?")
print(answer.text, answer.sources)
```

`records()` returns a generator that follows cursors, so the caller never handles paging.

**Anything else** — the OpenAPI document at `/v1/unified/openapi.json` generates a client in any
language. That is why FastAPI was chosen in Phase 00.

**JavaScript**, for completeness:

```js
const r = await fetch("http://localhost:8000/v1/unified/records?entity=customer&limit=50", {
  headers: { Authorization: "Bearer udal_live_..." },
});
const { data, sources } = await r.json();
```

## Sinks — push instead of pull

For consumers that want delivery rather than polling:

- `json_sink` / `csv_sink` — write a snapshot to a path.
- `webhook_sink` — POST batches to a URL on sync completion, with HMAC-SHA256 signing in an
  `X-Udal-Signature` header, retries with backoff, and a dead-letter record after N failures.

A webhook sends **real** data to a destination the user configured. It does not pass through the
anonymiser — and the UI must say so clearly when configuring one. The anonymiser protects
against the AI provider, not against the user's own chosen integrations.

---

## Validation Gate 09

**1. One request, every system**

With three connections synced (SQL + Sheets + the demo connector):

```bash
curl -s -H "Authorization: Bearer $KEY" \
  "http://127.0.0.1:8000/v1/unified/records?limit=100" | python -m json.tool
```

Assert: records from all three appear, every record has `source` and `lineage`, and `sources`
lists all three with `last_sync_at`. **This is the deliverable. Look at the output yourself.**

**2. An external process can consume it with no knowledge of the internals**

Write `examples/consume.py` that imports only `udal_client`, pulls every customer, and prints a
count per source. Run it from a **different directory** against the running server.

**3. The envelope is universal**

Assert a 404, a 401, a 429 and a validation error all return the same top-level keys with
`ok: false`.

**4. Key scoping is enforced in the query, not after it**

Create a key limited to `entities: ["customer"]` and `sources: ["conn_7"]`.

```bash
curl -s -H "Authorization: Bearer $LIMITED" "...?entity=invoice"      # -> 403
curl -s -H "Authorization: Bearer $LIMITED" "...?limit=1000"          # -> only conn_7 customers
```

Assert `page.total` reflects only the permitted scope. A `total` that counts rows the caller
cannot see is a leak.

**5. Revocation is immediate**

Revoke a key mid-session; the next request is 401 with no restart.

**6. Paging is complete and stable**

Pull 5,000 records at 100 per page via the client generator. Exactly 5,000 unique ids, no
duplicates. Insert rows *during* the pull and assert no previously returned id repeats.

**7. Streaming**

`?format=ndjson` on 50,000 records: memory stays flat (watch RSS) and the first byte arrives
before the query completes.

**8. A failed source is reported, not hidden**

Break one connection, then query. `ok` stays `true`, the healthy sources return data, and
`warnings` names the broken one. Silence here is the failure mode.

**9. OpenAPI is valid and generates a client**

```bash
curl -s http://127.0.0.1:8000/v1/unified/openapi.json > openapi.json
python -c "import json; s=json.load(open('openapi.json')); print(sorted(s['paths']))"
```

**10. Webhook signing**

Assert the receiver can verify `X-Udal-Signature` with the shared secret, and that a tampered
body fails verification.

---

## Common failures in this phase

| Symptom | Cause |
|---|---|
| Consumers write per-endpoint parsers | the envelope is not applied to errors |
| A restricted key sees restricted `total` | scoping applied as a post-filter |
| Pages repeat rows under write load | offset paging instead of keyset cursors |
| OOM on a large export | `format=json` building the whole list; use `ndjson` |
| A broken source silently returns fewer rows | no `warnings`, no partial-failure reporting |
| Revoked keys keep working | key verification cached without a TTL |
