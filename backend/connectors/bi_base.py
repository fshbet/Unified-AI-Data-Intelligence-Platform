"""Shared base for BI platforms that already own a semantic model (Power BI, Tableau).

These sources are different from a raw database: besides rows they carry *meaning* — measures with
formulas, declared relationships, field descriptions, hidden/technical flags. That model is the most
valuable thing they have, so the connector exposes it as a `SemanticModel` and the importer
(`backend.semantic.bi_import`) folds it into our own catalog instead of re-deriving it.

Row data is materialised into DuckDB like every other non-SQL source, so metrics, the investigation
engine, permissions and row limits all work unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.connectors.materialized import MaterializedConnector


@dataclass
class SemanticColumn:
    name: str
    data_type: str = "string"
    description: str | None = None
    is_hidden: bool = False
    format_string: str | None = None
    data_category: str | None = None  # e.g. "Date", "Currency" in Power BI; role in Tableau


@dataclass
class SemanticMeasure:
    """A governed measure as the BI tool defines it — formula kept verbatim."""

    name: str
    expression: str  # native formula (DAX / Tableau)
    table: str | None = None  # home table, when the tool records one
    description: str | None = None
    format_string: str | None = None
    is_hidden: bool = False
    folder: str | None = None


@dataclass
class SemanticRelationship:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    cardinality: str = "many_to_one"
    is_active: bool = True


@dataclass
class SemanticTable:
    name: str
    description: str | None = None
    is_hidden: bool = False
    columns: list[SemanticColumn] = field(default_factory=list)


@dataclass
class SemanticModel:
    name: str
    system: str  # "powerbi" | "tableau"
    language: str  # "dax" | "tableau"
    description: str | None = None
    tables: list[SemanticTable] = field(default_factory=list)
    measures: list[SemanticMeasure] = field(default_factory=list)
    relationships: list[SemanticRelationship] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name, "system": self.system, "language": self.language,
            "tables": len(self.tables), "columns": sum(len(t.columns) for t in self.tables),
            "measures": len(self.measures), "relationships": len(self.relationships),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "description": self.description,
            "table_detail": [{"name": t.name, "description": t.description, "is_hidden": t.is_hidden,
                              "columns": [c.__dict__ for c in t.columns]} for t in self.tables],
            "measure_detail": [m.__dict__ for m in self.measures],
            "relationship_detail": [r.__dict__ for r in self.relationships],
        }


class BIConnector(MaterializedConnector):
    """Materialised connector that can additionally hand over the source's own semantic model."""

    category = "bi"
    semantic_language: str = "dax"

    def fetch_semantic_model(self) -> SemanticModel:  # pragma: no cover - overridden
        raise NotImplementedError

    def semantic_model_safe(self) -> tuple[SemanticModel | None, str | None]:
        """Never let a metadata failure break a sync — the rows are still worth having."""
        try:
            return self.fetch_semantic_model(), None
        except Exception as e:  # noqa: BLE001
            return None, f"{type(e).__name__}: {e}"


def clean_name(raw: str) -> str:
    """'Sales[Total Revenue]' / '[Value]' -> 'Total Revenue' / 'Value' (Power BI row keys)."""
    s = str(raw)
    if "[" in s and s.endswith("]"):
        s = s[s.index("[") + 1 : -1]
    return s.strip()
