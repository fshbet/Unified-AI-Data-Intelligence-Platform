# Build Guide — Unified Data Access Layer (Python + SQLite)

A complete, phase-by-phase specification for building a single application that connects to
Google, Microsoft and SQL data products through pluggable connectors, merges everything into
one queryable place, and lets an AI reason over it **without ever seeing the real data**.

You are going to build this in the web-based Gemini portal, one phase at a time. This folder
is written for that: each phase file is self-contained, so you can paste one file into a fresh
Gemini session and get working code back without the model needing the rest of the repo.

---

## The four things this app must do

1. **Pure Python + SQLite.** No Node build step, no Postgres, no Redis, no message broker.
   One `pip install`, one command to run.
2. **Plug in anything.** Connectors, auth methods, anonymisation strategies, AI providers and
   output sinks are all plugins. Adding a new technology means adding one file, never editing
   the core.
3. **Many ways to authenticate.** Every connector supports more than one of: service account,
   browser-based OAuth, device code, stored credentials, and ambient/managed identity. The
   user picks per connection.
4. **The AI never sees real data.** Values are replaced with surrogate tokens before leaving
   the machine, and the model's answer is mapped back to the real values before anyone reads
   it. You choose which columns are protected.

Plus the delivery requirement: **one output contract**. Any external application points at a
single endpoint and gets everything, normalised, regardless of how many systems are behind it.

---

## How to use this with Gemini

**One phase per session.** Do not paste the whole folder. Each phase is sized to fit
comfortably in context along with the code it produces.

For each phase, in order:

1. Open a new Gemini session.
2. Paste `01-conventions.md` first — it is short and every phase depends on it.
3. Paste the phase file (e.g. `02-phase-00-foundations.md`).
4. Let it generate the files. Save them into your project at the paths the phase specifies.
5. **Run the gate.** Every phase ends with a *Validation Gate*: exact commands and exact
   expected output.
6. If the gate fails, paste the failure back into the same session and iterate. Do not move on.
7. When the gate passes, tick it off in `14-validation-gates.md` and start the next phase in a
   fresh session.

`15-gemini-prompts.md` has a ready-made prompt for each phase — copy, paste, go.

### Why a fresh session each phase

Long sessions drift: the model starts "improving" code you already validated, and you lose the
guarantee the gate gave you. A fresh session with the conventions file plus one phase keeps
each step honest. When a later phase needs an earlier contract, the phase file restates it.

---

## The rules you must hold the model to

The model will try to be helpful in ways that break this design. Push back every time:

| It will suggest | Say no because |
|---|---|
| Postgres, Redis, Celery, Docker | SQLite + threads is the requirement; it is enough for this workload |
| A React/Next frontend | Pure Python. Jinja2 + HTMX, server-rendered |
| SQLAlchemy ORM models everywhere | Plain SQL and `sqlite3.Row`; the schema is small and explicit |
| Putting connector logic in the core | Connectors are plugins; the core must not import them by name |
| Sending data to the AI "just for this one case" | There is no such case. The egress guard is unconditional |
| Hard-coding one auth method per provider | Every provider declares a *set* of supported auth modes |
| Skipping a gate because "it obviously works" | Two of the gates in this guide exist because it obviously didn't |

---

## Files

Read in this order. The numbers are the build order.

| File | What it is |
|---|---|
| `00-overview.md` | Architecture, the layer map, data flow, and the canonical record model |
| `01-conventions.md` | **Paste into every session.** Layout, style, deps, SQLite rules, plugin contracts |
| `02-phase-00-foundations.md` | Skeleton, config, SQLite, migration runner, health check |
| `03-phase-01-plugin-framework.md` | Registry, manifests, discovery, lifecycle |
| `04-phase-02-auth-and-secrets.md` | Encrypted vault, `AuthProvider`, all five auth modes |
| `05-phase-03-connector-sql.md` | Connector contract + SQLite/Postgres/MySQL/SQL Server |
| `06-phase-04-connector-google.md` | Sheets, BigQuery, Drive — service account, OAuth, ADC |
| `07-phase-05-connector-microsoft.md` | Graph, Excel, SharePoint, Azure SQL — SP, auth code, device code |
| `08-phase-06-unified-store.md` | Canonical records, ingestion, lineage, the "one place" |
| `09-phase-07-anonymization.md` | **The security boundary.** Policies, vault, egress guard, reversal |
| `10-phase-08-ai-gateway.md` | AI provider plugins and the anonymise → ask → restore pipeline |
| `11-phase-09-unified-api.md` | The single output contract and the embeddable client |
| `12-phase-10-frontend.md` | Server-rendered UI |
| `13-phase-11-packaging.md` | Install, run, embed, ship |
| `14-validation-gates.md` | Every gate in one checklist — your progress tracker |
| `15-gemini-prompts.md` | Copy-paste prompt per phase |
| `reference/anonymizer.py` | **Working, tested** reference implementation of Phase 07 |

### About `reference/anonymizer.py`

It runs today and its self-check passes:

```bash
python gemini-build/reference/anonymizer.py
```

```
all anonymiser checks passed
```

The anonymisation layer is the one part of this system where a subtle bug is a data breach
rather than a wrong number, so it was built and tested before being written up. Phase 07 walks
through the design; this file is the proof it works. Use it as the starting point rather than
asking Gemini to invent the algorithm from scratch.

---

## Build order, and why it is this order

Each layer is only built once the layer under it is proven, so a failure is always in the code
you just wrote:

```
Phase 00  foundations ......  it runs, it has a database
Phase 01  plugins .........  the core can load code it does not know about
Phase 02  auth ............  secrets are safe at rest and tokens refresh
Phase 03  SQL connectors ..  the first real data arrives, read-only
Phase 04  Google ..........  three auth modes against one provider
Phase 05  Microsoft .......  three more, proving the auth framework generalises
Phase 06  unified store ...  many sources become one shape          <- "all data in one place"
Phase 07  anonymisation ...  that shape can be made safe to send    <- the security boundary
Phase 08  AI gateway ......  the model answers, on tokens only
Phase 09  unified API .....  one endpoint any app can attach to     <- "single output"
Phase 10  frontend ........  a human can drive it
Phase 11  packaging .......  someone else can install it
```

Phases 04 and 05 can be skipped if you only need SQL sources; nothing later depends on them.
Everything else is a straight line — do not reorder.
