# Security

Written for the person who has to approve this tool. It states the limits as plainly as the
protections — overclaiming here is worse than underclaiming.

---

## Before you deploy

```bash
EDI_ENVIRONMENT=production            # makes the checks below fatal rather than advisory
EDI_SECRET_KEY=<48+ random chars>     # python -c "import secrets;print(secrets.token_urlsafe(48))"
EDI_DEFAULT_ADMIN_PASSWORD=<strong>
EDI_CORS_ORIGINS='["https://your-ui.example.com"]'
```

With `EDI_ENVIRONMENT=production` the app **refuses to start** if the secret key is a known
default, the admin password is `admin123`, or a `localhost` origin is still in the CORS list.
A warning would be ignored; a failed start will not be.

**`EDI_SECRET_KEY` signs JWTs and derives the Fernet key that encrypts every stored
credential.** Back it up. If you lose it, every saved connector password, OAuth secret and API
key must be re-entered — the app will tell you so rather than silently failing.

Also required: run the app behind TLS, bind it to localhost or a private network, and give each
database connection a **read-only** user.

### Upgrading an existing install

```bash
alembic upgrade head
python scripts/encrypt_existing_secrets.py --dry-run   # review
python scripts/encrypt_existing_secrets.py             # encrypt what was stored in plaintext
```

Before this release, secret detection used an exact-name match, which left `client_secret`,
`pat_secret`, `dsn`, `access_token` and `credentials_json` **unencrypted**. Detection is now
substring-based so a field added later is protected by default. Run the script once.

---

## The AI privacy boundary

Values from protected columns never reach an AI provider.

**What it does**

- Each column carries a strategy: `pseudonym` (reversible token), `redact`, `mask`, `hash`,
  `generalize` (bands), `shift` (dates moved by a per-job constant), or `passthrough`.
- Defaults come from the PII classifier that already runs during profiling, so names, emails,
  phone numbers, national IDs and salary are protected from first sync without configuration.
- Free text is scrubbed twice: against the values already tokenised from structured columns
  (which is how a customer name buried in a support note is caught), then with regex detectors
  for emails, phone numbers and account numbers.
- An **egress guard** runs on the fully serialised request body immediately before the HTTP
  call. If any original value survives, the request is aborted and audited; nothing is sent.
- The model's answer is mapped back to real values locally. Tokens the model invented are
  reported rather than silently printed.
- Tokens are scoped per job: stable within one answer, different in the next, so they never
  become a durable identifier a provider could correlate across sessions.

**What it does not do — read this part**

- It does not protect columns you set to `passthrough`. Numeric columns default to passthrough
  so the analysis engine still works; protect any number that is itself identifying.
- Values **shorter than three characters** are not covered by the egress guard. Two-character
  values collide with ordinary words and would make the guard fire on everything. Short
  sensitive values are covered by the column policy only.
- A sensitive value that appears **only in free text** and matches none of the detectors may not
  be caught — the dictionary pass can only find values it already knows.
- It does not apply to **webhooks or export sinks**, which send real data to destinations you
  configure. That is the point of those features; the UI says so when you configure one.
- Running a local model through Ollama means no data leaves the machine at all. That is the
  strongest option available and costs nothing.

**Vault retention.** De-anonymisation key material is destroyed after
`EDI_ANON_VAULT_RETENTION_HOURS` (default 24). After that an old answer can no longer be mapped
back to real values — by design.

---

## Python analysis is disabled by default

`EDI_PYTHON_ANALYSIS_ENABLED=false`.

The analysis tool executes **model-authored Python**. It runs in a subprocess with an import
allow-list, stripped builtins, no network, a timeout and a result cap — but that is a speed bump,
not a sandbox. Attribute traversal
(`().__class__.__base__.__subclasses__()`) reaches `subprocess.Popen`, which has been verified to
execute shell commands as the application process.

Enable it **only** when the app runs inside an isolated container:

```yaml
network_mode: none
read_only: true
cap_drop: [ALL]
security_opt: [no-new-privileges:true]
mem_limit: 512m
pids_limit: 64
```

While disabled, the tool is not advertised to the model at all.

---

## Data access

- Every SQL statement that reaches a user's database passes `validate_read_only`: single
  statement, `SELECT`/`WITH` only, comments stripped before parsing, DDL/DML and file/network
  functions rejected, and a row limit applied.
- Role-based access control with row-level filters (injected as filtered sub-selects) and
  column masking for non-admins.
- API keys for the unified API are hashed with SHA-256, shown once, and scoped by entity and
  source. Scoping is applied **inside** the query, so a restricted key cannot infer record
  counts for data it cannot read.
- Query history is scoped to its owner; only an admin sees other users' queries.

---

## Logging and retention

| Store | Contains | Default retention | Setting |
|---|---|---|---|
| `query_log.result_preview` | up to 20 result rows, masked for the executing user | 30 days, then the **rows** are dropped and the query text kept | `EDI_QUERY_PREVIEW_RETENTION_DAYS` |
| `anon_tokens` | de-anonymisation key material | 24 hours | `EDI_ANON_VAULT_RETENTION_HOURS` |
| `audit_logs` | who did what, when | 365 days | `EDI_AUDIT_RETENTION_DAYS` |

The purge runs hourly from the scheduler. Secrets are never logged; `Credential.__repr__` is
overridden so an unhandled exception cannot print a live token into a traceback.

**Known gap:** `chat.py` still writes the user's **raw** question to the audit log. If someone
types a real customer name into the question box, it is persisted in plaintext. Tracked as G-2.

---

## Error handling

Unhandled exceptions return `{"detail": "Internal server error", "reference": "<id>"}` and
nothing else. Exception text is not returned, because SQLAlchemy and database-driver errors
embed the failing SQL together with its bound parameters — which is to say, real row data.
Match the `reference` against the server log.

`LeakError` is never returned to a client in any form: its message names the values that leaked
so an operator can act, and that message belongs only in the log.

---

## Reporting a vulnerability

Open a private security advisory on the repository rather than a public issue.
