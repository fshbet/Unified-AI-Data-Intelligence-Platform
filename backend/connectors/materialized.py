"""Base for non-SQL sources (files, Redis, REST, MongoDB). Each source materialises its data into
a private DuckDB database so that the query engine can treat every source uniformly as SQL.
Large files are read by DuckDB directly (no pandas round-trip) so memory stays bounded."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterator

import duckdb
import pandas as pd

from backend.connectors.base import ColumnInfo, DataConnector, QueryResult, TableInfo
from backend.connectors.sql_connector import logical_type_of
from backend.core.config import settings


class MaterializedConnector(DataConnector):
    dialect = "duckdb"
    supports_sql = True

    @property
    def db_path(self) -> Path:
        return settings.duckdb_dir / f"{self.source_id}.duckdb"

    def _con(self, read_only: bool = True) -> duckdb.DuckDBPyConnection:
        if not self.db_path.exists():
            duckdb.connect(str(self.db_path)).close()
        return duckdb.connect(str(self.db_path), read_only=read_only)

    # subclasses implement one of these two
    def load_frames(self) -> Iterator[tuple[str, pd.DataFrame]]:  # pragma: no cover - overridden
        return iter(())

    def load_into(self, con: duckdb.DuckDBPyConnection) -> list[str]:
        """Default: materialise pandas frames. Override to let DuckDB read files natively."""
        names = []
        for name, df in self.load_frames():
            con.register("_tmp_df", df)
            con.execute(f"CREATE OR REPLACE TABLE {self.quote_ident(name)} AS SELECT * FROM _tmp_df")
            con.unregister("_tmp_df")
            names.append(name)
        return names

    def refresh(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        con = self._con(read_only=False)
        try:
            names = self.load_into(con)
        finally:
            con.close()
        return {"refreshed": True, "tables": names, "duration_ms": int((time.perf_counter() - t0) * 1000)}

    def test_connection(self) -> dict[str, Any]:
        try:
            self.refresh()
            return {"ok": True, "message": "Data loaded"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}

    def list_tables(self) -> list[TableInfo]:
        con = self._con()
        try:
            rows = con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main' ORDER BY 1").fetchall()
            out = []
            for (name,) in rows:
                n = con.execute(f"SELECT COUNT(*) FROM {self.quote_ident(name)}").fetchone()[0]
                out.append(TableInfo(name=name, row_count=int(n)))
            return out
        finally:
            con.close()

    def get_schema(self, table: str, schema: str | None = None) -> TableInfo:
        con = self._con()
        try:
            rows = con.execute(f"DESCRIBE {self.quote_ident(table)}").fetchall()
            n = con.execute(f"SELECT COUNT(*) FROM {self.quote_ident(table)}").fetchone()[0]
        finally:
            con.close()
        cols = [ColumnInfo(name=r[0], data_type=r[1], logical_type=logical_type_of(r[1]), nullable=(r[2] == "YES")) for r in rows]
        return TableInfo(name=table, columns=cols, row_count=int(n))

    def execute_query(self, query: str, limit: int | None = None, timeout: int | None = None) -> QueryResult:
        limit = limit or settings.max_result_rows
        q = query.strip().rstrip(";")
        if " limit " not in q.lower():
            q = f"SELECT * FROM ({q}) AS _q LIMIT {limit + 1}"
        t0 = time.perf_counter()
        con = self._con()
        try:
            df = con.execute(q).df()
        finally:
            con.close()
        return QueryResult.from_df(df.head(limit), int((time.perf_counter() - t0) * 1000), truncated=len(df) > limit)
