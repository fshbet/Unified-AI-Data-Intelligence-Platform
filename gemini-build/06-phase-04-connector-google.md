# Phase 04 — Google Connectors

**Goal:** read Google Sheets, BigQuery and Drive, with **three** working auth modes against one
provider. This is the phase that proves the Phase 02 framework was worth building.

**Depends on:** Phases 00–03. Skippable if you do not need Google sources — nothing later
depends on it.

```bash
pip install -e ".[google]"
```

---

## Build these files

```
udal/plugins/connectors/google_sheets.py
udal/plugins/connectors/bigquery.py
udal/plugins/connectors/google_drive.py
udal/connectors/google_common.py          shared client construction + scope handling
tests/stubs/google_stub.py
tests/integration/test_google_connectors.py
```

---

## The three auth modes, and when each is right

| Mode | Scenario | What the user supplies |
|---|---|---|
| `service_account` | Unattended server sync. The **default** for scheduled jobs | SA JSON key; share the Sheet/dataset with the SA's email |
| `oauth_code` | "Connect my Google account" — the user's own files, their own permissions | one browser click |
| `ambient` | Running on GCP (Cloud Run, GCE, GKE) | nothing — ADC/metadata server |

Tell the user, in the UI, the one thing that breaks service-account setups:

> A service account is a separate identity. Sharing the spreadsheet with
> `name@project.iam.gserviceaccount.com` is a required step — without it the connection
> authenticates fine and then returns "file not found".

Surface that explicitly: if Sheets returns 404 on a service-account connection, the error
message must say *"authenticated as <sa-email>; share the file with that address"*, not pass
through Google's unhelpful wording.

### Scopes

Request the minimum, read-only:

```python
SHEETS   = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
DRIVE    = ["https://www.googleapis.com/auth/drive.readonly"]
BIGQUERY = ["https://www.googleapis.com/auth/bigquery.readonly"]
```

Never request a read-write scope. If a token comes back with broader scopes than requested,
log it — it means the user consented to an older, wider grant and the connection should be
re-consented.

---

## `google_sheets.py`

```python
manifest = Manifest(
    key="google_sheets", kind="connector", display_name="Google Sheets",
    requires=["google"],
    config_fields=[
        Field("spreadsheet_id", "Spreadsheet ID",
              help="The long id in the URL: /spreadsheets/d/<THIS>/edit"),
        Field("header_row", "Header row", type="int", default=1, required=False),
        Field("sheets", "Sheets to include", required=False,
              help="Comma-separated; blank means all"),
    ],
    capabilities={"read_only": True, "paged": True},
)
supported_auth = ("service_account", "oauth_code", "ambient")
```

- `discover()` — `spreadsheets.get` for tab names and grid sizes. **One `DatasetInfo` per tab.**
- `read()` — `values.get` with `A1` ranges, paged by row window. The cursor is the next row
  index.
- Column names come from `header_row`. Handle the real-world mess: blank header cells become
  `column_7`, duplicates get `_2` suffixes, and leading/trailing spaces are stripped. Record the
  original header in `lineage["headers"]`.
- Type inference per column from sampled values → `int|float|bool|date|datetime|string`. Be
  conservative: one unparseable value makes the whole column `string`.
- `lineage` carries `{"spreadsheet_id", "sheet_name", "range", "row_offset"}` so any cell can be
  traced back to its tab and row.

**`valueRenderOption=UNFORMATTED_VALUE`** — otherwise you get the display string and `1,234.50`
arrives as text. Use `dateTimeRenderOption=FORMATTED_STRING` for dates, since Sheets' serial
numbers are more trouble than they are worth.

## `bigquery.py`

Config: `project_id`, `dataset_id`, optional `location`, `max_bytes_billed`.

- `discover()` — `INFORMATION_SCHEMA.TABLES` / `.COLUMNS` within the dataset.
- `read()` — parameterised `SELECT` with a page token; **route every query through
  `validate_read_only` from Phase 03**, same as any other SQL.
- Always set `maximumBytesBilled`. A connector that can run an unbounded scan against a billed
  API is a connector that will eventually produce a very expensive invoice. Default it to 1 GB
  and make it a config field.
- Use `dryRun` in `test()` to report the bytes a query would scan without running it.

## `google_drive.py`

Config: `folder_id`, `file_types` (csv/xlsx/json), `recursive`.

- `discover()` — list files of the supported types; one `DatasetInfo` per file.
- `read()` — download and parse. Stream to a temp file; do not hold a large export in memory.
- Google-native Sheets files found in Drive are delegated to the Sheets connector rather than
  exported, so the tab structure survives.

---

## Validation Gate 04

`tests/stubs/google_stub.py` speaks the real wire protocol over `http.server`: the OAuth token
endpoint, `spreadsheets.get`, `values.get`, and the BigQuery jobs/query endpoints. Point the
connectors at it by overriding the API base URL in config. **Do not mock the HTTP client** —
mocks will happily accept a request shape Google would reject.

**1. All three auth modes reach the same data**

```bash
pytest -q tests/integration/test_google_connectors.py -k auth
```

Assert that `service_account`, `oauth_code` and `ambient` each produce a working connector that
returns identical rows. That equivalence is the whole point of Phase 02.

**2. Sheets quirks are handled**

Fixture tab containing: a blank header cell, two identical headers, a header with trailing
spaces, an empty row in the middle, a numeric column with one text value, and a tab named
`Q3 Results (final)`.

- blank header → `column_<n>`
- duplicates → `revenue`, `revenue_2`
- `" Region "` → `Region`
- the mixed column is typed `string`, not `int`
- the spaced/parenthesised tab name round-trips through `discover()` and `read()`

**3. Paging**

A 5,000-row tab read in 1,000-row pages returns 5,000 unique rows and a `None` cursor at the end.

**4. The service-account 404 message is useful**

Make the stub return 404 for a service-account token. Assert the raised `ConnectorError`
contains the service account's email address and the word "share".

**5. BigQuery cannot be made to write, and cannot run unbounded**

```python
with pytest.raises(ConnectorError):
    bq.read_sql("DROP TABLE dataset.t")
```

Assert every outbound BigQuery job body contains `maximumBytesBilled`.

**6. Token refresh mid-sync**

Make the stub expire the token after the second page. A 5-page read must complete, refreshing
once, with no duplicated or dropped rows.

---

## Common failures in this phase

| Symptom | Cause |
|---|---|
| "File not found" on a valid Sheet id | the Sheet is not shared with the service account |
| Numbers arrive as `"1,234.50"` strings | missing `valueRenderOption=UNFORMATTED_VALUE` |
| Dates arrive as `45292` | Sheets serial numbers; use `FORMATTED_STRING` |
| Works for an hour then 401s | refresh token never issued — see Phase 02's `prompt=consent` |
| Columns shift after a blank header | header normalisation drops blanks instead of naming them |
| Enormous BigQuery bill | no `maximumBytesBilled` |
