# Phase 05 — Microsoft Connectors

**Goal:** read Microsoft 365 and Azure data sources, with **four** auth modes. If Phase 04
proved the auth framework generalises across modes, this proves it generalises across vendors.

**Depends on:** Phases 00–03. Skippable like Phase 04.

```bash
pip install -e ".[microsoft]"
```

---

## Build these files

```
udal/connectors/msal_common.py              token acquisition for all four modes
udal/plugins/connectors/ms_graph.py         Excel on OneDrive/SharePoint, Outlook, Lists
udal/plugins/connectors/sharepoint_list.py
udal/plugins/connectors/azure_sql.py        thin wrapper over the Phase 03 SQL connector
udal/plugins/connectors/dataverse.py
tests/stubs/microsoft_stub.py
tests/integration/test_microsoft_connectors.py
```

---

## The four auth modes

Microsoft's split between *application* and *delegated* permissions is the thing to get right —
it is the source of nearly every confusing 403 in this ecosystem.

| Mode | MSAL call | Permission type | Use for |
|---|---|---|---|
| `service_account` | `acquire_token_for_client` | **Application** — app acts as itself, admin consent required | unattended sync |
| `oauth_code` | `acquire_token_by_authorization_code` | **Delegated** — app acts as the signed-in user | "connect my account" |
| `device_code` | `acquire_token_by_device_flow` | Delegated | headless servers, CLI |
| `ambient` | `ManagedIdentityCredential` | Application | running inside Azure |

Also support `credentials` (ROPC, `acquire_token_by_username_password`) **only** as a
last resort, clearly labelled in the UI:

> Username/password sign-in does not work with multi-factor authentication or conditional
> access, and Microsoft discourages it. Use browser sign-in unless you have no choice.

### The permission trap, stated plainly in your docs and error messages

Application permissions (`Files.Read.All`) grant access to the **entire tenant**. Delegated
permissions (`Files.Read`) grant only what the signed-in user can already see. A connection
that works under `oauth_code` and 403s under `service_account` almost always means admin
consent was never granted. Detect that specific case and say so:

```
AuthError: the app registration has no admin consent for Files.Read.All.
An administrator must grant it in Entra ID > App registrations > API permissions.
```

### Scopes

Application mode uses `https://graph.microsoft.com/.default` — the scopes come from the app
registration, not the request. Delegated mode requests explicit read-only scopes:

```python
DELEGATED = ["Files.Read", "Sites.Read.All", "User.Read", "offline_access"]
```

`offline_access` is required or you get no refresh token.

---

## `msal_common.py`

```python
def acquire(mode: str, config: dict, state: dict) -> Credential
```

One function, four branches, returning the same `Credential`. Details that matter:

- **Use MSAL's token cache**, serialised into the Phase 02 vault. Do not re-acquire per request.
- Authority is `https://login.microsoftonline.com/{tenant_id}`; `common` for multi-tenant,
  `organizations` to exclude personal accounts. Make it configurable — getting this wrong
  produces `AADSTS50020`, which says nothing useful.
- Certificate credentials as well as secrets for `service_account`: a `.pem` plus thumbprint.
  Secrets expire and silently break scheduled syncs; certificates are the better default for
  production and are not much harder.
- Always read `error_description` from a failed response, not just `error` — the description
  carries the `AADSTS` code that identifies the actual problem.

---

## `ms_graph.py`

Config: `resource` (`excel`|`outlook`|`onedrive`), `drive_id`/`site_id`, `item_path`,
`worksheet`.

- **Excel** — `/workbook/worksheets/{name}/usedRange`. Same header normalisation as Sheets
  (Phase 04); reuse that code rather than writing it twice.
- **Outlook** — `/messages` with `$select` and `$top`; entity `event`. Read-only scope only.
- Paging follows `@odata.nextLink` **verbatim**. Do not rebuild the URL from parts — the token
  is opaque and reconstructing it breaks on the second page.
- Honour `Retry-After` on 429. Graph throttles aggressively and ignoring it gets the app
  temporarily blocked tenant-wide.

## `sharepoint_list.py`

`/sites/{site-id}/lists/{list-id}/items?expand=fields`. Map SharePoint's internal field names
(`Title`, `OData__x0020_Cost`) back to display names from the list's column metadata — the raw
internal names are unusable in an AI prompt or a UI.

## `azure_sql.py`

Subclass the Phase 03 SQL connector with `engine="mssql"` and token auth: acquire a bearer token
for `https://database.windows.net/.default` and pass it as an access token on the ODBC
connection. Everything else — safety validation, paging, dialect — is inherited. If you find
yourself copying SQL logic here, stop; it belongs in Phase 03.

## `dataverse.py`

OData v4 at `https://{org}.crm{n}.dynamics.com/api/data/v9.2/`. Entity metadata comes from
`EntityDefinitions`. Map `account`→`customer`, `contact`→`contact`, `opportunity`→`order`.

---

## Validation Gate 05

`tests/stubs/microsoft_stub.py` implements the MSAL token endpoint (client credentials,
auth code, device code, ROPC), the Graph workbook endpoints with `@odata.nextLink`, and a 429
response with `Retry-After`.

**1. Four modes, one result**

```bash
pytest -q tests/integration/test_microsoft_connectors.py -k auth
```

`service_account`, `oauth_code`, `device_code` and `credentials` must all return identical rows
from the same stub worksheet.

**2. Device code flow**

`begin_interactive()` returns a `user_code` and a verification URL. The stub answers
`authorization_pending` twice, then `slow_down`, then succeeds. Assert the poller survives all
three and that the interval increased after `slow_down`.

**3. Throttling is respected**

The stub returns 429 with `Retry-After: 2` once. Assert the connector waited roughly 2 seconds
and then succeeded — not that it failed, and not that it retried immediately.

**4. Paging follows nextLink exactly**

Stub returns `@odata.nextLink` with an opaque `$skiptoken`. Assert the second request URL is the
returned link **character for character**.

**5. The admin-consent error is diagnosed, not passed through**

Stub returns `AADSTS65001`. Assert the raised `AuthError` mentions admin consent and names the
permission — not a raw Microsoft error dump.

**6. Azure SQL inherits the read-only guarantee**

```python
with pytest.raises(ConnectorError):
    azure_sql.read_sql("DELETE FROM dbo.customers")
```

**7. SharePoint field names are readable**

Assert `OData__x0020_Cost` is surfaced as `Cost`.

---

## Common failures in this phase

| Symptom | Cause |
|---|---|
| 403 with app auth, works with user auth | admin consent never granted for the application permission |
| `AADSTS50020` | wrong authority: personal account against a tenant-specific authority |
| Second page returns the first page again | `@odata.nextLink` rebuilt instead of used verbatim |
| Sync dies after weeks, worked fine before | client secret expired — prefer a certificate |
| Tenant-wide throttling | `Retry-After` ignored |
| No refresh token in delegated mode | `offline_access` missing from scopes |
