# Phase 10 — Frontend

**Goal:** a human can connect a system, choose what to protect, and ask a question — without
touching a config file.

Pure Python: FastAPI + Jinja2 + HTMX. No Node, no build step, no `node_modules`.

**Depends on:** Phases 00–09.

---

## Why HTMX and not React

The entire UI is forms, tables and a chat box. HTMX is one ~14 KB vendored file; the server
returns HTML fragments; there is no build, no bundler and no second language. A React frontend
would add a toolchain larger than this application for a UI with eight pages.

Vendor `htmx.min.js` into `udal/web/static/` — do not load it from a CDN. The app must work on
an air-gapped machine, which is exactly the kind of place that cares about not sending data to
an AI provider.

---

## Build these files

```
udal/web/templates/base.html          layout, nav, theme
udal/web/templates/pages/*.html       one per page below
udal/web/templates/partials/*.html    HTMX fragments
udal/web/static/app.css
udal/web/static/htmx.min.js           vendored
udal/api/routers/ui.py
```

---

## Pages

| Route | Purpose |
|---|---|
| `/` | Dashboard: connection health, record counts by entity, recent syncs, protected-column count |
| `/connections` | List, add, test, sync, delete |
| `/connections/new` | **Form generated from the plugin manifest** |
| `/connections/{id}` | Datasets, enable/disable, sync history, errors |
| `/data` | Browse the unified store: entity filter, source filter, search, paging |
| `/privacy` | **The anonymisation policy editor** |
| `/ask` | The question interface |
| `/settings` | AI providers, API keys, audit log |

### `/connections/new` — generated, never hand-written

Pick a connector → the form renders **from `manifest.config_fields` alone** (Phase 01). Pick an
auth mode from `supported_auth` → the auth plugin's fields are appended.

If you find yourself writing a template branch per connector, the plugin architecture has
failed. One template, one loop over fields, one `<input>` per `Field.type`. Adding a connector
in two years must produce a working form with zero frontend changes — gate test 2 checks this.

Interactive auth: **Connect with Google/Microsoft** opens the redirect from
`POST /auth/start` in a popup; the callback closes it and HTMX swaps in the updated status.
Device code shows the user code and polls.

### `/privacy` — the page that sells the product

A table of every column across every dataset:

| Source | Dataset | Column | Sample (masked) | Detected | Strategy | Preview |
|---|---|---|---|---|---|---|
| CRM Export | Q3 | `customer_name` | `Pr***` | person | `pseudonym` ▾ | `[[PER_0001]]` |
| CRM Export | Q3 | `revenue` | `48200` | — | `passthrough` ▾ | `48200` |
| HR DB | staff | `ssn` | `12***` | national id | `redact` ▾ | `[[REDACTED]]` |

- Detected strategies arrive pre-selected from `detect.py` with a **"review before enabling"**
  banner. They are proposals, never silently active.
- The **Preview** column is the important one: it shows exactly what the AI would see for a real
  sample value, updated live as the dropdown changes. This is how a non-technical owner gains
  confidence, and it is worth the effort.
- Bulk actions: "protect all detected", "protect all text columns in this dataset".
- A banner counts unprotected columns containing detected PII, linking straight to them.
- Sample values are **masked in the UI too**. A privacy screen that displays everyone's real
  email address to anyone who opens it is self-defeating.

### `/ask`

- Question box, answer streamed back (SSE), evidence table underneath.
- A persistent badge: **"N columns protected · M rows sent · tokens only"** from the
  `anonymization` block of the API response. Visible on every answer.
- If `unresolved_tokens` is non-empty, show a clear warning that the model referenced something
  not in the data, and do not present the answer as verified.
- Each evidence row links to its source via `lineage`.

> **SSE note:** the server sends `\r\n` line endings; a client that splits on `\n\n` will parse
> nothing and the stream will appear silently broken. Normalise with
> `text.replace(/\r\n/g, "\n")` before splitting. This costs an hour to find.

---

## Design

Keep it plain and legible — this is an admin tool, not a marketing site.

- System font stack. No webfont, no CDN.
- CSS custom properties on `:root`, with a `prefers-color-scheme: dark` block. Give `body` an
  explicit background in both.
- One accent colour. Red reserved exclusively for destructive actions and leak warnings.
- Tables: tabular numerals (`font-variant-numeric: tabular-nums`), right-aligned numbers.
- Every destructive action confirms, naming the thing being deleted.
- Works at 375 px wide. Tables scroll horizontally in a wrapper rather than breaking the page.

---

## Validation Gate 10

**1. The full journey, by hand, on a clean database**

Connect a source → test → sync → see records in `/data` → set a column to `pseudonym` in
`/privacy` → ask a question in `/ask` → see a real name in the answer and a "columns protected"
badge. No terminal, no config file.

**2. A new connector needs no frontend work**

Drop a new connector file into `plugins/connectors/`, restart, and confirm it appears in
`/connections/new` with a correct, complete form. Then:

```bash
grep -rn "google_sheets\|azure_sql\|bigquery" udal/web/templates/
```

Must print nothing.

**3. Both interactive auth flows complete in a browser**

OAuth popup and device code, each against the Phase 04/05 stubs.

**4. The privacy preview is truthful**

Set a column to `pseudonym`. Ask a question. Capture the outbound AI request. The value shown in
the Preview column must match what actually went over the wire. If the preview and reality ever
disagree, the page is worse than not having it.

**5. No secret reaches the browser**

```bash
curl -s http://127.0.0.1:8000/connections/conn_7 | grep -ci "hunter2\|BEGIN PRIVATE KEY"
```

Must be `0`. Check the HTML source and every HTMX fragment.

**6. It works offline**

Disconnect the network. Load every page. No CDN request, no webfont, no console error. Confirm
in the browser's network tab that every request is same-origin.

**7. Responsive and accessible**

375 px: no horizontal page scroll. Keyboard: tab to every control, visible focus ring. Labels
bound to inputs. Colour is never the only signal — a protected column shows an icon or text as
well as a colour.

**8. Errors are actionable**

Break a connection's credentials. `/connections` must show *"Token expired — reconnect"* with a
working button, not a stack trace and not a bare `500`.

---

## Common failures in this phase

| Symptom | Cause |
|---|---|
| Per-connector template branches | not rendering from `config_fields` |
| The answer stream never appears | SSE `\r\n` vs `\n\n` — see the note above |
| Secrets visible in HTML | a form pre-filled with the decrypted value; render `***` and only send on change |
| OAuth popup hangs | the callback never signals the opener; `postMessage` then close |
| Privacy page leaks the data it protects | unmasked sample values |
| Page breaks offline | CDN `<script>` or Google Fonts link |
