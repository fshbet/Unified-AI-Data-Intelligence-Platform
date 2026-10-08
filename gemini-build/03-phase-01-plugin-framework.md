# Phase 01 — Plugin Framework

**Goal:** the core can load, describe and instantiate code it does not know about. After this
phase, adding a new technology is one file in `plugins/` and zero changes to the core.

**Depends on:** Phase 00.

---

## Build these files

```
udal/plugins/base.py            the five ABCs + Manifest/Field (from the conventions file)
udal/plugins/registry.py        discovery, registration, lookup, availability
udal/plugins/__init__.py
udal/plugins/connectors/__init__.py
udal/plugins/connectors/demo_connector.py    a trivial connector that proves the loop
udal/api/routers/plugins.py     GET /v1/plugins
tests/unit/test_registry.py
```

---

## `udal/plugins/base.py`

Exactly the dataclasses and ABCs from `01-conventions.md`. Add the small value types they
reference:

```python
@dataclass(frozen=True)
class TestResult:
    ok: bool
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class DatasetInfo:
    name: str                       # table / sheet / collection / endpoint
    entity: str = "other"
    row_estimate: int | None = None
    columns: list[ColumnInfo] = field(default_factory=list)

@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str = "string"            # string|int|float|bool|date|datetime|json
    nullable: bool = True

@dataclass(frozen=True)
class Page:
    records: list[dict[str, Any]]
    next_cursor: str | None = None
    lineage: dict[str, Any] = field(default_factory=dict)
```

---

## `udal/plugins/registry.py`

```python
def register(cls):                                  # decorator
def autoload() -> None:                             # import every plugin module
def all_plugins(kind: str | None = None) -> list[Manifest]
def get(kind: str, key: str) -> type                # raises ConfigError if unknown
def create(kind: str, key: str, config: dict) -> Any
def availability(manifest: Manifest) -> tuple[bool, str | None]
```

### Discovery

1. Walk every subpackage of `udal/plugins/` with `pkgutil.iter_modules` and import each module.
2. Then walk every directory in `settings.plugin_paths` the same way, so a user can drop a
   plugin beside the app without reinstalling.
3. `@register` adds the class to `_REGISTRY[(kind, key)]`.

### The autoload guard — read this carefully

```python
_LOADED = False

def autoload() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    ...
```

**Guard on an explicit flag, never on `_REGISTRY` being non-empty.** Any module that imports a
single plugin directly — and the API layer will — makes the registry non-empty, and a
"skip if already populated" check then silently suppresses every plugin that had not been
imported yet. The symptom is the UI showing a handful of connectors instead of all of them,
while every unit test passes, because each test process imports fresh. This has happened; the
gate below tests for it specifically.

### Import failures must not be fatal

A plugin whose optional extra is missing still registers, marked unavailable:

```python
try:
    importlib.import_module(name)
except ImportError as exc:
    _UNAVAILABLE[name] = str(exc)      # recorded, logged at INFO, never raised
```

`availability()` returns `(False, "pip install udal[google]")` for those, and the UI greys them
out with that hint. An unavailable plugin must still expose its `Manifest` so the user can see
what they are missing.

---

## `udal/plugins/connectors/demo_connector.py`

A connector with no dependencies that returns three fixed rows. Its only job is to prove the
whole loop — discovery, manifest, config form, instantiation, read — before any real system is
involved. Keep it in the shipped package; it is the fastest way to debug the framework later.

```python
@register
class DemoConnector(Connector):
    manifest = Manifest(
        key="demo", kind="connector", display_name="Demo (built-in sample data)",
        config_fields=[Field("row_count", "Rows to generate", type="int", default=3, required=False)],
        capabilities={"read_only": True, "paged": False},
    )
    supported_auth = ("none",)
```

---

## `GET /v1/plugins`

```json
{
  "plugins": [
    {
      "key": "demo", "kind": "connector", "display_name": "Demo (built-in sample data)",
      "version": "1.0.0", "available": true, "install_hint": null,
      "config_fields": [{"name":"row_count","label":"Rows to generate","type":"int","required":false,"default":3}],
      "capabilities": {"read_only": true, "paged": false},
      "supported_auth": ["none"]
    }
  ]
}
```

Supports `?kind=connector`. **`config_fields` is the contract the UI builds its forms from** —
Phase 10 generates every connection form from this and nothing else, so no form is ever
hand-written per connector.

---

## Validation Gate 01

**1. The demo connector is discovered and usable**

```bash
python -c "
from udal.plugins import registry
registry.autoload()
print([m.key for m in registry.all_plugins('connector')])
c = registry.create('connector', 'demo', {'row_count': 3})
print(c.test().ok, len(c.read('sample').records))
"
```

```
['demo']
True 3
```

**2. The autoload regression — this is the important one**

```bash
python -c "
import udal.plugins.connectors.demo_connector      # import ONE plugin first
from udal.plugins import registry
registry.autoload()
keys = {m.key for m in registry.all_plugins('connector')}
assert 'demo' in keys, keys
print('ok', sorted(keys))
"
```

Must list every connector, not just the pre-imported one. Add this as a permanent test in
`tests/unit/test_registry.py` — it is cheap and it catches a bug that costs hours.

**3. A broken plugin degrades, it does not crash**

Drop a file into `udal/plugins/connectors/` containing `import nonexistent_sdk`, then:

```bash
curl -s http://127.0.0.1:8000/v1/plugins | python -m json.tool
```

The app must still start and still serve every other plugin. The broken one either does not
appear or appears with `"available": false`. Delete the file afterwards.

**4. External plugin path works**

Put a connector in `./extra_plugins/my_connector.py`, set
`UDAL_PLUGIN_PATHS='["./extra_plugins"]'`, restart, and confirm it appears in `/v1/plugins`.

**5. The core is clean**

```bash
grep -rn "from udal.plugins.connectors" udal/core udal/api udal/store 2>/dev/null
```

Must print nothing. If the core imports a specific connector, the plugin architecture is
decorative. The only allowed import is `udal.plugins.registry`.

**6. Tests**

```bash
pytest -q tests/unit/test_registry.py
```

Cover: every expected key is discoverable; autoload completes after a single early import;
`get()` on an unknown key raises `ConfigError`; an unavailable plugin still returns a manifest;
`create()` rejects a config missing a required field.

---

## Common failures in this phase

| Symptom | Cause |
|---|---|
| Only some connectors appear in the UI | the `_LOADED` guard — see above |
| App won't start after adding a connector | an import error escaped `autoload`'s try/except |
| A plugin registers twice | `autoload` ran per-request instead of once at startup |
| The UI form is missing fields | `config_fields` incomplete; the UI must render from the manifest alone |
