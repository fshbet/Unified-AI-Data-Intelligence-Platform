# Phase 11 — Packaging, Embedding & Hardening

**Goal:** someone who is not you can install this, run it, attach their application to it, and
trust it with production credentials.

**Depends on:** every previous phase.

---

## Build these files

```
pyproject.toml              finalised: extras, entry points, package data
udal/cli.py                 init / serve / sync / key / doctor
examples/consume.py         minimal external consumer
examples/embed_fastapi.py   mounting udal inside an existing app
docs/INSTALL.md
docs/SECURITY.md
docs/API.md                 generated from the OpenAPI document
.env.example
Dockerfile                  optional
tests/integration/test_fresh_install.py
```

---

## The CLI

```bash
udal init                   # create data dir, generate secret key, run migrations
udal serve                  # the app
udal sync [--connection X]  # headless sync, for cron
udal key create --name "analytics" --scopes read --entities customer
udal doctor                 # diagnose a broken install
```

`udal doctor` is worth the hour. Check and report, each pass/fail with a fix:

- Python version, writable data directory, database reachable, migrations current
- `secret_key` present and stable (warn loudly if it looks regenerated — every stored credential
  is now undecryptable)
- Which optional extras are installed, and which plugins are therefore unavailable
- Each connection: credential valid / expired / needs re-auth
- AI provider reachable
- **Anonymisation self-test** — run a known row through anonymise → guard → de-anonymise and
  assert it round-trips. If this fails, the install is unsafe and `doctor` must say so in those
  words.

---

## Embedding in an existing application

Two supported ways, both tested:

**1. As a service** — run it, call `/v1/unified/*` with an API key. The default.

**2. Mounted into an existing FastAPI app:**

```python
# examples/embed_fastapi.py
from fastapi import FastAPI
from udal.api.app import create_app

app = FastAPI()
app.mount("/data-layer", create_app())
```

Everything must work under a path prefix: `root_path`-aware URL generation, OAuth redirect URIs
built from the request rather than hard-coded, and static asset paths relative. Hard-coded
absolute paths are the usual reason mounting breaks — gate test 4 catches it.

**3. As a library**, for in-process use:

```python
from udal import Udal
udal = Udal(database="./data/udal.db")
for r in udal.records(entity="customer"):
    ...
```

---

## Security hardening checklist

Walk this before anyone points it at production. Each line is a real failure mode.

**Secrets**
- [ ] `secret_key` from the environment in production; never committed; rotation documented
- [ ] Every credential encrypted at rest; `grep` the DB file for a known password returns nothing
- [ ] No secret in any log, traceback, API response or HTML page
- [ ] Database file permissions `0600`

**Data access**
- [ ] All user SQL passes `validate_read_only`; verified through the HTTP path, not just unit tests
- [ ] Read-only database users documented as the recommended setup
- [ ] API keys scoped by entity and source; scoping applied inside the query
- [ ] Rate limiting on `/v1/unified/*`

**The AI boundary**
- [ ] `assert_no_leak` runs on the serialised body inside the provider base class
- [ ] Only `gateway.py` imports an AI provider (`grep` proves it)
- [ ] `LeakError` is never caught broadly — `grep -rn "except Exception" udal/ai/ udal/anonymize/`
- [ ] A provider call without a vault raises
- [ ] The audit log stores the **anonymised** question
- [ ] `ai_egress_enabled=false` blocks everything

**Network**
- [ ] Binds `127.0.0.1` by default; `0.0.0.0` requires an explicit opt-in and warns
- [ ] CORS restricted to `/v1/unified/*` with a configured origin list
- [ ] TLS termination documented for any non-localhost deployment

**Operational**
- [ ] `udal doctor` passes on a fresh install
- [ ] Backup = copy the DB file (document: **stop the app or use `VACUUM INTO`**; copying a
      live WAL database produces a corrupt backup)
- [ ] Losing `secret_key` means re-entering every credential — stated in INSTALL.md

---

## `docs/SECURITY.md`

Write this for the person who has to approve the tool. Be specific about the boundary **and its
limits** — overclaiming here is worse than underclaiming:

- What anonymisation does: replaces configured column values with reversible local tokens;
  scans free text using regex detectors plus the known-values dictionary; blocks any payload
  still containing an original.
- What it does **not** do: it does not protect columns you set to `passthrough`; it cannot
  recognise a sensitive value that appears **only** in free text and matches no detector; values
  shorter than three characters are not covered by the egress guard; and it does not apply to
  webhook sinks, which send real data to destinations you configure.
- Where the vault lives, how it is encrypted, when it is purged (default: 24 hours after the job,
  configurable, and immediately on request).
- That a local model via Ollama means no data leaves the machine at all — the strongest option
  for users who need it.

---

## Validation Gate 11

**1. Clean-machine install**

Fresh venv, fresh clone, no existing database:

```bash
python -m venv .venv && .venv/bin/pip install -e .
udal init && udal serve
```

Must reach a working `/health` with no manual steps. Best done in a container or on a second
machine — your development box has state you have forgotten about.

**2. Doctor catches real breakage**

Corrupt `secret_key`, then run `udal doctor`. It must fail clearly, name the problem, and say
credentials must be re-entered. Also assert the anonymisation self-test fails loudly if you
deliberately break `deanonymize`.

**3. External consumer**

From another directory, `python examples/consume.py` pulls records and prints per-source counts.

**4. Mounted under a path prefix**

Run `examples/embed_fastapi.py`. Every page loads, static assets resolve, and the OAuth
redirect URI contains `/data-layer`.

**5. The whole security checklist above passes**, each item checked by running something.

**6. Full suite green**

```bash
pytest -q
```

**7. Fresh end-to-end rehearsal**

Delete the database. Then: install → init → add a connection → sync → set a privacy policy →
issue an API key → consume from an external script → ask a question → confirm the captured AI
request contains tokens only. This is the acceptance test for the entire project.

---

## When to stop

The app is done when a new data source is one file, a new AI provider is one file, and the
answer to *"could this send our customer names to Google?"* is a test you can run in front of
the person asking.
