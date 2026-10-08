# 01 — Conventions

**Paste this file at the start of every Gemini session, before the phase file.** It is kept
short on purpose: it is the shared contract every phase depends on.

---

## Project

- Package `udal`, Python 3.11+, type hints everywhere, `from __future__ import annotations`.
- Pure Python. **No Node, no build step, no Docker requirement, no Postgres, no Redis, no Celery.**
- Storage is **SQLite only**. Background work is `ThreadPoolExecutor` plus a `jobs` table.
- Core dependencies, nothing else without asking:
  `fastapi uvicorn pydantic pydantic-settings jinja2 httpx cryptography python-multipart`
- Connector SDKs are **optional extras** (`.[google]`, `.[microsoft]`, `.[postgres]`). A missing
  extra must never break import — the plugin registers as unavailable with an install hint.

## Style

- Plain SQL with `sqlite3`, parameterised. No ORM. Rows come back as `sqlite3.Row`.
- Dataclasses for internal models; Pydantic only at the API boundary.
- Functions do one thing. No class with a single method — make it a function.
- Comments explain *why*, never *what*. No docstring that restates the signature.
- No speculative abstraction: no interface with one implementation, no config for a constant.
- Every non-trivial module ends with a runnable self-check (`demo()` under `__main__`) or has a
  matching `tests/unit/test_<module>.py`. Whichever is smaller.

## SQLite rules

Non-negotiable, these cause real bugs:

```python
conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=WAL")     # concurrent readers during a sync
conn.execute("PRAGMA foreign_keys=ON")      # OFF by default in SQLite
conn.execute("PRAGMA busy_timeout=30000")
```

- One connection per thread. Never share a connection across threads.
- `timeout=30` or long syncs produce `database is locked`.
- Commit before handing work to a thread pool; do not hold a write transaction across I/O.
- Datetimes are stored as ISO-8601 UTC strings. JSON blobs as TEXT.

## Errors

```python
class UdalError(Exception): ...
class ConfigError(UdalError): ...        # bad settings, missing extra
class AuthError(UdalError): ...          # expired, denied, needs re-consent
class ConnectorError(UdalError): ...     # upstream system failed
class PolicyError(UdalError): ...        # anonymisation policy invalid
class LeakError(UdalError): ...          # SECURITY: original data about to escape. Never caught broadly.
```

API handlers map these to status codes. `LeakError` is never swallowed, never retried, never
downgraded to a warning — it aborts the request and is written to the audit log.

## Security rules that apply in every phase

1. Secrets are encrypted at rest with Fernet, never written to logs, never returned by an API.
   Any API response containing a secret field returns `"***"`.
2. All SQL that reaches a user's database is **read-only**. Validate before execution: single
   statement, must start with `SELECT` or `WITH`, reject
   `INSERT UPDATE DELETE DROP ALTER TRUNCATE CREATE GRANT ATTACH PRAGMA` and any
   file/network function. Apply a `LIMIT`.
3. The anonymisation egress guard is unconditional. There is no flag, no "internal" caller and
   no debug mode that skips it.
4. Every outbound request to a user system or AI provider is audited: who, what, when, which
   connection, how many rows, how many tokens.

## Logging

Structured, one JSON object per line, via `core/logging.py`. Never log a secret, a token's
original value, or a full row. Log counts and ids.

---

## The five plugin kinds

Everything extensible in this app is one of these. All five live under `plugins/`, all five are
discovered the same way (Phase 01), all five declare a `Manifest`.

```python
@dataclass(frozen=True)
class Manifest:
    key: str                      # "google_sheets" — unique, stable, snake_case
    kind: str                     # auth | connector | anonymizer | ai | sink
    display_name: str             # "Google Sheets"
    version: str = "1.0.0"
    requires: list[str] = field(default_factory=list)   # pip extras needed
    config_fields: list[Field] = field(default_factory=list)  # the UI builds a form from this
    capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    type: str = "string"          # string|password|int|bool|select|file|textarea
    required: bool = True
    default: Any = None
    choices: list[str] | None = None
    help: str | None = None
    secret: bool = False          # encrypted at rest, masked in responses
```

### 1. `AuthProvider` — how to obtain a credential

```python
class AuthProvider(ABC):
    manifest: Manifest
    mode: str                     # service_account|oauth_code|device_code|credentials|ambient

    @abstractmethod
    def acquire(self, config: dict, state: dict) -> Credential: ...
    def refresh(self, cred: Credential) -> Credential: return cred
    def revoke(self, cred: Credential) -> None: return None
    def begin_interactive(self, config: dict) -> InteractiveStart | None: return None
    def complete_interactive(self, config: dict, payload: dict) -> Credential:
        raise NotImplementedError
```

`begin_interactive`/`complete_interactive` are what make browser and device-code flows possible
without blocking the server. Non-interactive modes leave them alone.

### 2. `Connector` — how to read a system

```python
class Connector(ABC):
    manifest: Manifest
    supported_auth: tuple[str, ...]        # e.g. ("service_account", "oauth_code", "ambient")

    @abstractmethod
    def test(self) -> TestResult: ...
    @abstractmethod
    def discover(self) -> list[DatasetInfo]: ...
    @abstractmethod
    def read(self, dataset: str, cursor: str | None = None, limit: int = 1000) -> Page: ...
    def entity_for(self, dataset: str) -> str: return "other"
```

`read` is always paged and always read-only. `Page` carries `records`, `next_cursor`, `lineage`.

### 3. `AnonymizerPlugin` — a strategy or a detector

```python
class AnonymizerPlugin(ABC):
    manifest: Manifest
    def strategies(self) -> dict[str, Strategy]: return {}
    def detectors(self) -> list[tuple[str, re.Pattern[str]]]: return []
```

### 4. `AIProvider` — a model

```python
class AIProvider(ABC):
    manifest: Manifest
    @abstractmethod
    def complete(self, messages: list[dict], **opts) -> Completion: ...
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError
```

The provider receives already-anonymised messages. It must never be given the vault, the policy
set or a database handle — that is how the boundary stays impossible to cross by accident.

### 5. `Sink` — where unified output can be pushed

```python
class Sink(ABC):
    manifest: Manifest
    @abstractmethod
    def emit(self, records: Iterable[Record], opts: dict) -> EmitResult: ...
```

---

## Testing

- `pytest`. Unit tests need no network and no credentials.
- Every external system is faked by a **stub server in `tests/stubs/`** that speaks the real
  wire protocol over `http.server`. Do not mock the HTTP client — mocks hide protocol mistakes.
- The same stubs back the demo scripts, so documentation cannot drift from what is tested.
- Security behaviour gets an explicit negative test: assert the bad thing is *refused*, not just
  that the good path works.

## Definition of done for every phase

1. The code runs.
2. The gate commands produce the stated output.
3. `pytest` is green.
4. No secret is readable in the database or any log.
5. Nothing in the core imports a plugin by name.
