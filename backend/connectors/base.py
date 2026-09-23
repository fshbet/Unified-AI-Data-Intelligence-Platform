"""Connector plugin interface. Every data source type implements DataConnector and
registers itself; the core never imports a specific database driver."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pandas as pd


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    logical_type: str = "string"  # number|integer|string|date|datetime|boolean|json
    nullable: bool = True
    is_primary_key: bool = False
    is_foreign_key: bool = False
    fk_target: str | None = None  # "schema.table.column"


@dataclass
class TableInfo:
    name: str
    schema: str | None = None
    row_count: int | None = None
    columns: list[ColumnInfo] = field(default_factory=list)
    comment: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    duration_ms: int = 0
    truncated: bool = False

    def to_df(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows, columns=self.columns)

    @classmethod
    def from_df(cls, df: pd.DataFrame, duration_ms: int = 0, truncated: bool = False) -> "QueryResult":
        return cls(columns=[str(c) for c in df.columns], rows=df.astype(object).where(df.notna(), None).values.tolist(), duration_ms=duration_ms, truncated=truncated)


@dataclass
class ConfigField:
    name: str
    label: str
    type: str = "string"  # string|password|integer|boolean|select|textarea|file
    required: bool = True
    default: Any = None
    options: list[str] | None = None
    help: str | None = None


class DataConnector(ABC):
    """Common interface. Subclasses set `type_key`, `display_name`, `dialect`, `config_fields`."""

    type_key: ClassVar[str]
    display_name: ClassVar[str]
    category: ClassVar[str] = "database"  # database | file | nosql | api
    dialect: ClassVar[str] = "generic"  # SQL dialect used by query planner
    config_fields: ClassVar[list[ConfigField]] = []
    supports_sql: ClassVar[bool] = True

    def __init__(self, source_id: str, config: dict[str, Any]):
        self.source_id = source_id
        self.config = config

    # ---- lifecycle
    @abstractmethod
    def test_connection(self) -> dict[str, Any]:
        """Return {"ok": bool, "message": str, ...}."""

    def close(self) -> None:  # noqa: B027
        pass

    # ---- discovery
    def list_databases(self) -> list[str]:
        return []

    def list_schemas(self) -> list[str]:
        return []

    @abstractmethod
    def list_tables(self) -> list[TableInfo]:
        """Tables without column detail."""

    @abstractmethod
    def get_schema(self, table: str, schema: str | None = None) -> TableInfo:
        """Full column detail for a table."""

    # ---- data
    @abstractmethod
    def execute_query(self, query: str, limit: int | None = None, timeout: int | None = None) -> QueryResult:
        """Execute read-only SQL in this connector's dialect."""

    def sample_data(self, table: str, schema: str | None = None, n: int = 50) -> QueryResult:
        return self.execute_query(f"SELECT * FROM {self.quote_table(table, schema)}", limit=n)

    def get_row_count(self, table: str, schema: str | None = None) -> int:
        r = self.execute_query(f"SELECT COUNT(*) AS n FROM {self.quote_table(table, schema)}")
        return int(r.rows[0][0]) if r.rows else 0

    def get_statistics(self, table: str, schema: str | None = None, sample_rows: int = 100_000) -> pd.DataFrame:
        """Return a (possibly sampled) DataFrame for profiling. Profiling logic lives in metadata.profiler."""
        return self.execute_query(f"SELECT * FROM {self.quote_table(table, schema)}", limit=sample_rows).to_df()

    # ---- helpers
    def quote_ident(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def quote_table(self, table: str, schema: str | None = None) -> str:
        return f"{self.quote_ident(schema)}.{self.quote_ident(table)}" if schema else self.quote_ident(table)

    def refresh(self) -> dict[str, Any]:
        """Re-materialise data for non-SQL sources (files/redis/api). No-op for live DBs."""
        return {"refreshed": False}
