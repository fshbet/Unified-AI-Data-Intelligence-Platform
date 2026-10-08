# Phase 08 — AI Gateway

**Goal:** the model answers questions over the unified store, having seen only tokens. This
phase is mostly *wiring*, and the wiring is the security property: it must be impossible to
call an AI provider without passing through the anonymiser.

**Depends on:** Phases 00–02, 06, 07.

---

## Build these files

```
udal/ai/gateway.py                  the pipeline — the only way to reach a provider
udal/ai/prompts.py
udal/ai/context.py                  rows -> compact prompt context
udal/plugins/ai/gemini.py
udal/plugins/ai/openai_compatible.py    OpenAI, Azure OpenAI, Ollama, vLLM
udal/plugins/ai/anthropic.py
udal/migrations/005_ai.sql
udal/api/routers/ask.py
tests/integration/test_ai_gateway.py
tests/stubs/ai_stub.py
```

---

## The one rule of this phase

```python
# udal/ai/gateway.py — the ONLY module permitted to import an AI provider.
def ask(conn, question: str, scope: Scope, actor: str) -> Answer:
    job     = new_job(conn)                              # fresh salt, fresh vault
    rows    = store.query(conn, **scope.as_query())      # real values
    policies= policy.for_datasets(conn, scope.datasets)
    safe    = anonymizer.anonymize_rows(rows, policies)  # pass 1
    prompt  = prompts.build(question, safe)              # question is scrubbed too
    answer  = provider.complete(prompt)                  # guard fires inside the client
    text, unresolved = anonymizer.deanonymize(answer.text, vault)
    audit(conn, actor, job, rows_sent=len(safe), unresolved=unresolved)
    return Answer(text=text, evidence=rows, unresolved=unresolved, ...)
```

Structural rules that make the boundary hold:

1. **Only `gateway.py` imports a provider.** Nothing else, ever. Gate test 5 greps for this.
2. **A provider plugin never receives the vault, the policy set, or a database connection.** It
   gets a list of messages and returns text. It is structurally incapable of leaking, because it
   has nothing to leak.
3. **The egress guard lives inside the provider base class**, on the serialised request body,
   immediately before the HTTP call — not in `gateway.py`. A new provider added in a year
   inherits the guard without its author thinking about it.
4. **The user's question is anonymised too.** Someone will type *"why did Priya Raman churn?"*.
   Run `scrub_text` over the question before it goes into the prompt.

### Where the guard goes, concretely

```python
class BaseAIProvider(AIProvider):
    def complete(self, messages, *, vault=None, **opts):
        body = self._build_body(messages, **opts)
        payload = json.dumps(body)
        if vault is not None:
            assert_no_leak(payload, vault)       # <- unconditional, last thing before the wire
        elif settings.require_vault:
            raise LeakError("AI call attempted without an anonymisation vault")
        return self._post(payload)
```

That `elif` matters: a caller who forgets to pass the vault gets a `LeakError`, not a silent
plaintext send. Fail closed on the plumbing, even though the policy layer fails open.

---

## `udal/ai/context.py`

Turning rows into prompt text is where cost and quality are decided.

- Prefer a **compact table** (CSV-ish or markdown) over JSON. JSON spends 2–3× the tokens on
  punctuation and repeated keys for the same information.
- Include the column list with types once, at the top, instead of repeating keys per row.
- Cap rows by token budget, not row count. Say explicitly in the prompt when truncation
  happened: `"showing 200 of 4,812 matching rows"`. A model told it has everything will
  confidently generalise from a truncated sample.
- Include the per-source `last_sync_at` from Phase 06, so the model can qualify stale data.

## `udal/ai/prompts.py`

The system prompt must state the honesty rules, because the model cannot infer them:

```
You are analysing data that has been anonymised. Identifiers appear as tokens such as
[[PER_0001]] or [[ORG_0003]]. Treat each token as a distinct real entity and refer to it by
its token exactly as written — do not invent names, do not reformat tokens, and never invent
a token that does not appear in the data.

Rules:
- Every factual claim must be supported by the rows provided. If the data does not support an
  answer, say so plainly.
- Correlation is not causation. Say "is associated with", not "caused".
- If the data was truncated, say your answer is based on a sample.
- Do not estimate a value you were not given.
```

## Provider plugins

All implement `AIProvider` from the conventions.

| Plugin | Covers | Config |
|---|---|---|
| `gemini.py` | Gemini API | `api_key`, `model` (default `gemini-2.0-flash`) |
| `openai_compatible.py` | OpenAI, Azure OpenAI, **Ollama**, vLLM, LM Studio | `base_url`, `api_key`, `model` |
| `anthropic.py` | Claude | `api_key`, `model` |

One `openai_compatible` plugin covers four backends because they share a wire format — do not
write four. Ollama is worth supporting well: a local model means data never leaves the machine
at all, which some users will require regardless of anonymisation.

API keys go in the Phase 02 vault. Usage (tokens in/out, latency, estimated cost) is recorded
per request in `ai_requests`.

## `udal/migrations/005_ai.sql`

```sql
CREATE TABLE IF NOT EXISTS ai_providers (
    id         TEXT PRIMARY KEY,
    plugin_key TEXT NOT NULL,
    name       TEXT NOT NULL,
    config     TEXT NOT NULL DEFAULT '{}',   -- non-secret only
    purposes   TEXT NOT NULL DEFAULT '[]',   -- ["answering","embedding","suggestion"]
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_requests (
    id            TEXT PRIMARY KEY,
    at            TEXT NOT NULL,
    provider_id   TEXT REFERENCES ai_providers(id),
    actor         TEXT,
    question      TEXT,                      -- the ANONYMISED question
    rows_sent     INTEGER NOT NULL DEFAULT 0,
    tokens_in     INTEGER, tokens_out INTEGER,
    latency_ms    INTEGER,
    unresolved    TEXT,                      -- JSON list of hallucinated tokens
    guard_blocked INTEGER NOT NULL DEFAULT 0,
    ok            INTEGER NOT NULL DEFAULT 1,
    error         TEXT
);
```

Store the **anonymised** question, not the original. The audit table must not become the place
the real data finally leaks.

---

## Validation Gate 08

`tests/stubs/ai_stub.py` is an HTTP server that **records every request body it receives** and
returns a canned completion. Capturing the real body is the point — this is how you prove the
boundary rather than assuming it.

**1. The provider received no original value** — the central test

```python
ask(conn, "why did Priya Raman churn?", scope, actor="t")
body = ai_stub.last_request_body
for original in ["Priya Raman", "priya@acme.com", "123-45-6789"]:
    assert original not in body
assert "[[PER_" in body          # and it did receive tokens
```

**2. The question itself was anonymised**

Assert `"Priya Raman"` is absent from the body even though the user typed it.

**3. The answer comes back de-anonymised**

Stub returns `"[[PER_0001]] churned in [[ORG_0002]]."`; the final answer contains the real name
and organisation, and no `[[` remains.

**4. A leak is blocked, loudly**

Monkeypatch the anonymiser to pass a value through. Assert:
`LeakError` is raised, **no HTTP request reached the stub**, and `ai_requests.guard_blocked = 1`.
This is the most important assertion in the suite — it proves the guard is reached on the real
path, not just in its own unit test.

**5. No bypass exists**

```bash
grep -rn "import httpx\|requests.post\|urllib" udal/plugins/ai/ | grep -v base
grep -rln "plugins.ai" udal/ --include=*.py | grep -v "udal/ai/gateway.py\|udal/plugins/ai"
```

The second command must print nothing: only `gateway.py` may reference AI plugins.

**6. Calling a provider without a vault fails closed**

```python
with pytest.raises(LeakError):
    provider.complete(messages)        # no vault passed
```

**7. Truncation is disclosed**

Query 5,000 rows with a budget that fits 200. Assert the prompt says so and the answer is
qualified.

**8. Kill switch**

`UDAL_AI_EGRESS_ENABLED=false` → `ask()` raises cleanly and no request is made.

**9. Hallucinated tokens surface**

Stub returns `[[PER_0099]]`. `Answer.unresolved` contains it and the API response flags the
answer as unverified.

**10. A real provider, once**

Run one live question against Gemini or a local Ollama model. Then re-read the captured request
in your provider's debug log and confirm with your own eyes that it contains tokens and no real
values. Automated tests prove the code path; look at it once yourself.

---

## Common failures in this phase

| Symptom | Cause |
|---|---|
| Real data in the provider's request | guard applied to rows, not to the serialised body |
| Guard never fires in production | it lives in `gateway.py`, and some path calls the provider directly |
| Tokens in the user-visible answer | `deanonymize` skipped on the streaming path |
| The model invents names | prompt does not say to use tokens verbatim |
| Costs 3× what you expected | rows serialised as JSON instead of a compact table |
| The audit log contains real values | logging the original question instead of the scrubbed one |
