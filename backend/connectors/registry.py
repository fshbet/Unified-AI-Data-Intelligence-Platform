"""Connector registry. Adding a connector = subclass DataConnector + @register."""
from __future__ import annotations

import importlib
import pkgutil
from typing import Any

from backend.connectors.base import DataConnector

_REGISTRY: dict[str, type[DataConnector]] = {}
_LOADED = False


def register(cls: type[DataConnector]) -> type[DataConnector]:
    _REGISTRY[cls.type_key] = cls
    return cls


def _autoload() -> None:
    # Guard on an explicit flag, not on `_REGISTRY` being empty: any module that imports a single
    # connector (e.g. `from backend.connectors.file_connector import SUPPORTED`) would otherwise make
    # the registry non-empty and silently suppress every connector that had not been imported yet.
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    import backend.connectors as pkg

    for m in pkgutil.iter_modules(pkg.__path__):
        if m.name not in {"base", "registry"}:
            importlib.import_module(f"backend.connectors.{m.name}")


def get_connector_class(type_key: str) -> type[DataConnector]:
    _autoload()
    if type_key not in _REGISTRY:
        raise KeyError(f"Unknown connector type '{type_key}'. Available: {sorted(_REGISTRY)}")
    return _REGISTRY[type_key]


def create_connector(type_key: str, source_id: str, config: dict[str, Any]) -> DataConnector:
    return get_connector_class(type_key)(source_id, config)


def list_connector_types() -> list[dict[str, Any]]:
    _autoload()
    return [
        {
            "type": c.type_key,
            "display_name": c.display_name,
            "category": c.category,
            "dialect": c.dialect,
            "supports_sql": c.supports_sql,
            "fields": [f.__dict__ for f in c.config_fields],
        }
        for c in sorted(_REGISTRY.values(), key=lambda c: (c.category, c.display_name))
    ]
