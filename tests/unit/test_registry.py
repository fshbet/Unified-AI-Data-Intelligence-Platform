"""The connector registry must expose every connector even when one was imported first.

Regression test: `_autoload` used to skip loading if the registry was already non-empty, so a module
importing a single connector (the API does exactly that) left most connectors invisible at runtime.
"""
import importlib
import sys


ALL_TYPES = {"postgresql", "mysql", "mssql", "sqlite", "oracle", "duckdb",
             "file", "redis", "mongodb", "rest_api", "graphql", "powerbi", "tableau"}


def _fresh_registry():
    for name in [m for m in sys.modules if m.startswith("backend.connectors")]:
        del sys.modules[name]
    return importlib.import_module("backend.connectors.registry")


def test_all_connectors_are_discoverable():
    reg = _fresh_registry()
    assert {t["type"] for t in reg.list_connector_types()} >= ALL_TYPES


def test_autoload_completes_even_if_a_connector_was_imported_first():
    reg = _fresh_registry()
    importlib.import_module("backend.connectors.file_connector")  # populates the registry early
    assert {t["type"] for t in reg.list_connector_types()} >= ALL_TYPES
    for key in ALL_TYPES:
        assert reg.get_connector_class(key).type_key == key


def test_bi_connectors_are_categorised_for_the_ui():
    reg = _fresh_registry()
    bi = {t["type"]: t for t in reg.list_connector_types() if t["category"] == "bi"}
    assert set(bi) == {"powerbi", "tableau"}
    for t in bi.values():
        assert t["dialect"] == "duckdb" and t["supports_sql"]
        assert t["fields"], "the UI builds its form from these fields"
