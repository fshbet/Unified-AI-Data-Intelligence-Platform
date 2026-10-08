# 14 — Master Validation Checklist

Your progress tracker. Tick a phase only when **every** box under it passes. A phase that is
"basically working" is a phase that will cost you a day in three phases' time.

Full detail for each gate is in its phase file.

---

## Phase 00 — Foundations
- [ ] `pip install -e .` then `python run.py` starts and logs applied migrations
- [ ] `/health` returns `{"status":"ok", ...}`
- [ ] Second start applies **0** migrations; a failing migration leaves the DB unchanged
- [ ] `PRAGMA foreign_keys` reports `1` on a fresh connection
- [ ] The log filter redacts nested secret-looking keys
- [ ] `pytest -q` green

## Phase 01 — Plugin framework
- [ ] The demo connector is discovered, instantiated and read from
- [ ] **Autoload completes after a single plugin is imported first** (the `_LOADED` flag)
- [ ] A plugin with a missing dependency degrades to `available: false`; the app still starts
- [ ] An external `plugin_paths` directory is scanned
- [ ] `grep -rn "from udal.plugins.connectors" udal/core udal/api udal/store` prints nothing
- [ ] `/v1/plugins` returns `config_fields` complete enough to build a form

## Phase 02 — Auth & secrets
- [ ] No plaintext secret anywhere in the DB file (`grep` the raw file)
- [ ] API responses mask every secret as `"***"`
- [ ] All five modes work against the stub: service account, oauth code, device code, credentials, ambient
- [ ] OAuth rejects a bad/expired `state` without calling the token endpoint; PKCE present
- [ ] Device flow survives `authorization_pending` and backs off on `slow_down`
- [ ] 10 concurrent `get_credential` calls trigger **one** refresh
- [ ] A failed refresh sets `needs_auth` and raises `AuthError`, not a 500
- [ ] No secret in any log, including tracebacks

## Phase 03 — SQL connectors
- [ ] Safety validator accepts valid SELECT/WITH, including keyword-named columns and keywords inside string literals
- [ ] Rejects stacked statements, comment-hidden statements, DDL/DML, file and network functions
- [ ] Writes impossible via the Python object **and** via the HTTP route
- [ ] `Order Details` table with a `select` column discovers and reads correctly
- [ ] 2,500 rows in pages of 1,000: unique, complete, `next_cursor is None` at the end
- [ ] Statement timeout applied per engine

## Phase 04 — Google *(skip if unused)*
- [ ] `service_account`, `oauth_code` and `ambient` all return identical rows
- [ ] Blank/duplicate/spaced headers normalised; mixed-type column typed `string`
- [ ] 5,000-row tab pages correctly
- [ ] Service-account 404 names the SA email and says "share"
- [ ] BigQuery always sets `maximumBytesBilled`; DDL rejected
- [ ] Token expiring mid-sync refreshes without losing or duplicating rows

## Phase 05 — Microsoft *(skip if unused)*
- [ ] Four modes return identical rows
- [ ] Device flow handles pending + `slow_down`
- [ ] `Retry-After` on 429 respected
- [ ] `@odata.nextLink` used verbatim
- [ ] `AADSTS65001` diagnosed as missing admin consent, naming the permission
- [ ] Azure SQL inherits read-only enforcement
- [ ] SharePoint internal field names mapped to display names

## Phase 06 — Unified store
- [ ] **One query returns records from two different systems, each with source + lineage**
- [ ] Re-sync of unchanged data: `rows_changed == 0`, no duplicates
- [ ] One changed cell in 1,000 rows → `rows_changed == 1`
- [ ] Full sync soft-deletes; **incremental sync deletes nothing**
- [ ] Filters, FTS search and keyset paging correct over 2,500 records
- [ ] Malicious filter names rejected; `records` table still exists afterwards
- [ ] `sources` reports `last_sync_at` per connection
- [ ] Three concurrent syncs, no `database is locked`

## Phase 07 — Anonymisation  ← **the critical gate**
- [ ] `python reference/anonymizer.py` prints `all anonymiser checks passed`
- [ ] Round trip restores originals exactly
- [ ] `passthrough` column byte-identical; unpoliced numeric still a number
- [ ] Same value → same token within a job; different token in a new job
- [ ] **`assert_no_leak` raises on a planted original and names it**
- [ ] Free text: dictionary pass catches a name; detector catches an email in no column
- [ ] `[[ per_1 ]]` and `[[PER-0002]]` both resolve
- [ ] Hallucinated `[[PER_0099]]` returned in `unresolved`, not silently dropped
- [ ] `shift` preserves intervals; `generalize` preserves magnitude
- [ ] Vault `original` encrypted at rest
- [ ] 500-row fuzz with quotes, accents, emoji and regex metacharacters passes

## Phase 08 — AI gateway  ← **the critical gate**
- [ ] **Captured provider request contains no original value and does contain tokens**
- [ ] The user's question is anonymised before being sent
- [ ] The answer returns de-anonymised, with no `[[` left
- [ ] A planted leak raises `LeakError`, **no HTTP request is made**, `guard_blocked = 1`
- [ ] Only `gateway.py` references AI plugins (`grep` proves it)
- [ ] `complete()` without a vault raises `LeakError`
- [ ] Truncation disclosed in the prompt and the answer
- [ ] `ai_egress_enabled=false` blocks all calls
- [ ] Audit log stores the anonymised question
- [ ] One live run inspected **by eye**

## Phase 09 — Unified API
- [ ] One request returns data from three systems with `source`, `lineage` and `sources`
- [ ] An external script using only `udal_client` consumes it from another directory
- [ ] Errors use the same envelope as successes
- [ ] A scoped key cannot reach other entities/sources, and `page.total` respects the scope
- [ ] Revocation takes effect immediately
- [ ] 5,000 records page cleanly; no repeats when rows are inserted mid-pull
- [ ] `format=ndjson` streams with flat memory
- [ ] A broken source appears in `warnings` rather than silently shrinking the result
- [ ] `/v1/unified/openapi.json` is valid
- [ ] Webhook HMAC verifies; a tampered body fails

## Phase 10 — Frontend
- [ ] Full journey with no terminal: connect → sync → browse → protect → ask
- [ ] A new connector gets a working form with **zero** template changes
- [ ] OAuth popup and device code both complete in a browser
- [ ] The privacy Preview matches the captured outbound request exactly
- [ ] No secret in any HTML or HTMX fragment
- [ ] Works fully offline — no CDN, no webfont
- [ ] 375 px with no horizontal scroll; keyboard navigable; focus visible
- [ ] Expired credentials show "reconnect", not a stack trace

## Phase 11 — Packaging
- [ ] Clean-machine install reaches `/health` with no manual steps
- [ ] `udal doctor` diagnoses a corrupted secret key and a broken anonymiser
- [ ] `examples/consume.py` works from another directory
- [ ] Mounting under `/data-layer` works, including OAuth redirect URIs and static assets
- [ ] Every box in the Phase 11 security checklist
- [ ] `pytest -q` fully green
- [ ] Full fresh rehearsal: install → connect → sync → protect → key → consume → ask

---

## The four that actually matter

If you are short on time, never skip these. Each one is the difference between a demo and a
product:

1. **07 — the guard raises on a planted original.** Proves the boundary exists.
2. **08 — the captured provider request contains no real value.** Proves the boundary is
   reached on the real path, not just in its own test.
3. **06 — one query, two systems, with lineage.** Proves "all data in one place" is real.
4. **09 — an external script consumes everything through one endpoint.** Proves the product
   is attachable, which is the entire point.
