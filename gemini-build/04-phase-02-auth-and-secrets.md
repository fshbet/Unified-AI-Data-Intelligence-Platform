# Phase 02 — Authentication & Secret Vault

**Goal:** one framework that covers every way the supported systems authenticate, so a
connector declares *which* modes it supports and never implements any of them.

This is the phase that satisfies the requirement that Google, Microsoft and database products
can each be connected by **service account, browser-based login, or stored credentials**.

**Depends on:** Phases 00–01.

---

## The five auth modes

| Mode | Used by | Interactive? | Expires? |
|---|---|---|---|
| `service_account` | Google SA JSON, Azure app registration (secret or certificate) | no | token yes, key no |
| `oauth_code` | Google user consent, Microsoft delegated — browser redirect + PKCE | **yes** | yes, refresh token |
| `device_code` | Microsoft/Google on a headless box — user types a code on their phone | **yes** | yes, refresh token |
| `credentials` | SQL databases, REST APIs — username/password, DSN, API key | no | no |
| `ambient` | GCP ADC, Azure Managed Identity, Windows Integrated auth | no | token yes |

A connector declares `supported_auth = ("service_account", "oauth_code", "ambient")`. The UI
offers exactly those, and the chosen one drives which config fields are shown.

---

## Build these files

```
udal/core/crypto.py                      Fernet wrapper, key derivation
udal/auth/vault.py                       encrypted secret storage
udal/auth/models.py                      Credential, InteractiveStart
udal/auth/service.py                     connection CRUD + credential lifecycle
udal/plugins/auth/service_account.py
udal/plugins/auth/oauth_code.py
udal/plugins/auth/device_code.py
udal/plugins/auth/credentials.py
udal/plugins/auth/ambient.py
udal/api/routers/connections.py
udal/migrations/002_auth.sql
tests/unit/test_vault.py
tests/integration/test_auth_modes.py
tests/stubs/oauth_stub.py
```

---

## `udal/core/crypto.py`

```python
def fernet() -> Fernet          # key derived once from settings.secret_key
def encrypt(plaintext: str) -> str
def decrypt(ciphertext: str) -> str
```

Derive with HKDF-SHA256 over `secret_key` and a fixed info string, then urlsafe-b64 to 32 bytes.
Do **not** use the raw secret as the Fernet key and do not hand-roll anything else here.

---

## `udal/migrations/002_auth.sql`

```sql
CREATE TABLE IF NOT EXISTS connections (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    connector_key TEXT NOT NULL,
    auth_mode     TEXT NOT NULL,
    config        TEXT NOT NULL DEFAULT '{}',   -- JSON, NON-secret fields only
    status        TEXT NOT NULL DEFAULT 'new',  -- new|connected|needs_auth|error
    last_error    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- Every secret in the system lives here and nowhere else. Always ciphertext.
CREATE TABLE IF NOT EXISTS secrets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    ciphertext    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (connection_id, name)
);

CREATE TABLE IF NOT EXISTS credentials (
    connection_id TEXT PRIMARY KEY REFERENCES connections(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,            -- bearer|basic|api_key|dsn|sa_key
    ciphertext    TEXT NOT NULL,            -- the access token / password / DSN
    refresh_ct    TEXT,                     -- encrypted refresh token
    expires_at    TEXT,
    scopes        TEXT,
    metadata      TEXT NOT NULL DEFAULT '{}',
    updated_at    TEXT NOT NULL
);

-- Short-lived. An entry proves a callback belongs to a flow this server started.
CREATE TABLE IF NOT EXISTS oauth_states (
    state         TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    code_verifier TEXT NOT NULL,
    redirect_uri  TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);
```

---

## `udal/auth/models.py`

```python
@dataclass
class Credential:
    kind: str                       # bearer|basic|api_key|dsn|sa_key
    secret: str                     # held in memory only; encrypted the moment it is stored
    expires_at: datetime | None = None
    refresh_token: str | None = None
    scopes: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, skew: int = 120) -> bool:
        return self.expires_at is not None and \
               self.expires_at <= datetime.now(timezone.utc) + timedelta(seconds=skew)

    def __repr__(self) -> str:      # keeps secrets out of tracebacks and logs
        return f"Credential(kind={self.kind!r}, expires_at={self.expires_at!r})"


@dataclass(frozen=True)
class InteractiveStart:
    mode: str                       # "redirect" | "device"
    url: str
    user_code: str | None = None    # device flow only
    expires_in: int | None = None
    poll_interval: int | None = None
```

The custom `__repr__` is not cosmetic — without it the first unhandled exception prints a live
access token into the log.

---

## The auth plugins

### `service_account.py`

Config: `credentials_json` (secret, textarea or file upload) for Google; or
`tenant_id`/`client_id`/`client_secret` (secret) for Microsoft; optional `scopes`, `subject`
(domain-wide delegation).

`acquire()` builds a signed JWT assertion and exchanges it for an access token. `refresh()`
re-mints — service accounts have no refresh token, so refresh is just acquire again.

Validate the JSON key at save time and reject it with a clear message if `private_key` or
`client_email` is missing. A bad key discovered at sync time is far more annoying.

### `oauth_code.py` — the browser flow

Config: `client_id`, `client_secret` (secret), `authorize_url`, `token_url`, `scopes`,
`redirect_uri` (default `http://127.0.0.1:8000/v1/connections/{id}/oauth/callback`).

```python
def begin_interactive(self, config) -> InteractiveStart:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    # persist (state, verifier, redirect_uri) with a 10-minute expiry
    ...
```

**PKCE is mandatory**, even with a client secret. **State is mandatory** and must be verified on
callback, single-use, and deleted immediately after. A callback whose state is unknown or
expired is rejected with 400 — never "helpfully" exchanged anyway.

Request `access_type=offline` and `prompt=consent` for Google, or no refresh token comes back
on the second authorisation and the connection silently dies in an hour.

### `device_code.py`

`begin_interactive()` POSTs to the device-authorization endpoint and returns the verification
URL plus the user code. The caller polls `complete_interactive()`, which must treat
`authorization_pending` and `slow_down` as *not yet* rather than as errors — and on `slow_down`,
increase the interval. Give up at `expires_in`.

### `credentials.py`

Config: `username`, `password` (secret), or `api_key` (secret) + `header_name`, or a full
`dsn` (secret). No network call: `acquire()` wraps the stored value in a `Credential`. `test()`
is what proves it works, in the connector.

### `ambient.py`

No config. Tries, in order: `GOOGLE_APPLICATION_CREDENTIALS` / `google.auth.default()`,
Azure `DefaultAzureCredential`, then the platform's integrated auth. Raises `AuthError` with a
list of what it tried when nothing is available — "no ambient credentials found" with no detail
is a miserable thing to debug.

---

## `udal/auth/service.py`

```python
def create_connection(conn, name, connector_key, auth_mode, config, secrets_in) -> str
def get_credential(conn, connection_id) -> Credential   # refreshes transparently if expired
def start_interactive(conn, connection_id) -> InteractiveStart
def complete_interactive(conn, connection_id, payload) -> None
def delete_connection(conn, connection_id) -> None      # cascades, and revokes upstream first
```

`get_credential` is the only way anything in the app obtains a credential. It:

1. loads and decrypts,
2. refreshes if `is_expired()` (with a lock, so ten parallel reads do not run ten refreshes),
3. re-encrypts and stores the new token,
4. sets connection status to `needs_auth` and raises `AuthError` if refresh fails.

---

## API

```
POST   /v1/connections                      create (secrets in the body, never echoed back)
GET    /v1/connections                      list — every secret field is "***"
POST   /v1/connections/{id}/test            run the connector's test()
POST   /v1/connections/{id}/auth/start      -> InteractiveStart (redirect URL or device code)
GET    /v1/connections/{id}/oauth/callback  the redirect target; verifies state, exchanges code
POST   /v1/connections/{id}/auth/poll       device flow polling
DELETE /v1/connections/{id}
```

---

## Validation Gate 02

**1. Secrets are genuinely encrypted at rest**

```bash
python -c "
import sqlite3
c = sqlite3.connect('data/udal.db')
rows = list(c.execute('SELECT ciphertext FROM secrets'))
assert rows, 'create a connection with a secret first'
assert all('hunter2' not in r[0] for r in rows), 'PLAINTEXT SECRET IN DATABASE'
print('ok, ciphertext only:', rows[0][0][:40], '...')
"
```

Also grep the raw file — this catches a secret written somewhere other than `secrets`:

```bash
grep -c "hunter2" data/udal.db || echo "ok: not present anywhere in the database file"
```

**2. Secrets never come back out of the API**

```bash
curl -s http://127.0.0.1:8000/v1/connections | grep -c hunter2
```

Must be `0`. Every secret field reads `"***"`.

**3. All five modes work against the stub**

`tests/stubs/oauth_stub.py` is an `http.server` implementing the token, device-code and
authorize endpoints. `tests/integration/test_auth_modes.py` must prove:

- `service_account` — JWT assertion exchanged, bearer token returned.
- `oauth_code` — `begin_interactive` returns a URL containing `code_challenge` and `state`;
  the callback exchanges the code; a refresh token is stored.
- `oauth_code` **rejects a bad state** with 400 and does not call the token endpoint.
- `device_code` — two `authorization_pending` responses then success; the poller handles both,
  and `slow_down` increases the interval.
- `credentials` — round-trips a password and a DSN.
- `ambient` — raises a listing `AuthError` when nothing is configured.

**4. Refresh is transparent and happens once**

Store a credential expiring in 10 seconds. Call `get_credential` from 10 threads at once.
Assert the stub's token endpoint was hit **once** and all ten callers got the new token.

**5. Expired refresh degrades correctly**

Make the stub return `invalid_grant`. `get_credential` must raise `AuthError`, set the
connection status to `needs_auth`, and the UI must offer re-authentication — not a 500.

**6. No secret reaches a log**

```bash
python run.py 2>&1 | tee run.log
# create a connection with password hunter2, then:
grep -c hunter2 run.log
```

Must be `0`, including in tracebacks. This is what `Credential.__repr__` is for.

---

## Common failures in this phase

| Symptom | Cause |
|---|---|
| Connection dies after ~1 hour, needs re-login | no refresh token: missing `access_type=offline`/`prompt=consent` |
| `invalid_grant` on every callback | `redirect_uri` at the token step differs from the authorize step; they must match exactly |
| Device flow errors immediately | `authorization_pending` treated as a failure |
| Secrets readable in the DB | encrypting on read instead of on write, or a secret stored in `connections.config` |
| Ten refreshes per sync | no lock around refresh |
| Works for one Google account, breaks for the second | a token cached on the **class** rather than the instance — cache per connection |
