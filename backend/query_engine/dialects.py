"""Dialect-specific SQL fragments so generated SQL is native to each database."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Dialect:
    name: str
    quote_open: str = '"'
    quote_close: str = '"'

    def ident(self, name: str) -> str:
        return f"{self.quote_open}{name}{self.quote_close}"

    def table(self, table: str, schema: str | None = None) -> str:
        return f"{self.ident(schema)}.{self.ident(table)}" if schema else self.ident(table)

    def month_trunc(self, col: str) -> str:
        c = self.ident(col)
        return {
            "postgresql": f"DATE_TRUNC('month', {c})",
            "duckdb": f"DATE_TRUNC('month', CAST({c} AS DATE))",
            "mysql": f"DATE_FORMAT({c}, '%Y-%m-01')",
            "mssql": f"DATEFROMPARTS(YEAR({c}), MONTH({c}), 1)",
            "sqlite": f"strftime('%Y-%m-01', {c})",
            "oracle": f"TRUNC({c}, 'MM')",
        }.get(self.name, f"DATE_TRUNC('month', {c})")

    def month_key(self, col: str) -> str:
        """Expression that yields 'YYYY-MM' as text."""
        c = self.ident(col)
        return {
            "postgresql": f"TO_CHAR({c}, 'YYYY-MM')",
            "duckdb": f"strftime(CAST({c} AS DATE), '%Y-%m')",
            "mysql": f"DATE_FORMAT({c}, '%Y-%m')",
            "mssql": f"FORMAT({c}, 'yyyy-MM')",
            "sqlite": f"strftime('%Y-%m', {c})",
            "oracle": f"TO_CHAR({c}, 'YYYY-MM')",
        }.get(self.name, f"TO_CHAR({c}, 'YYYY-MM')")

    def date_between(self, col: str, start: str, end_exclusive: str) -> str:
        c = self.ident(col)
        if self.name == "oracle":
            return f"{c} >= DATE '{start}' AND {c} < DATE '{end_exclusive}'"
        if self.name in {"duckdb", "postgresql"}:
            return f"CAST({c} AS DATE) >= DATE '{start}' AND CAST({c} AS DATE) < DATE '{end_exclusive}'"
        return f"{c} >= '{start}' AND {c} < '{end_exclusive}'"

    def limit(self, sql: str, n: int) -> str:
        if self.name == "mssql":
            return f"SELECT TOP {n} * FROM ({sql}) AS _q"
        if self.name == "oracle":
            return f"SELECT * FROM ({sql}) FETCH FIRST {n} ROWS ONLY"
        return f"{sql} LIMIT {n}"

    def hints(self) -> str:
        """Short cheat-sheet included in LLM prompts."""
        return {
            "postgresql": "PostgreSQL: DATE_TRUNC('month', col), TO_CHAR(col,'YYYY-MM'), col::date, LIMIT n.",
            "duckdb": "DuckDB (PostgreSQL-like): DATE_TRUNC('month', CAST(col AS DATE)), strftime(col,'%Y-%m'), LIMIT n. Identifiers with double quotes.",
            "mysql": "MySQL: DATE_FORMAT(col,'%Y-%m'), backtick identifiers, LIMIT n.",
            "mssql": "SQL Server: DATEFROMPARTS(YEAR(col),MONTH(col),1), FORMAT(col,'yyyy-MM'), [bracket] identifiers, SELECT TOP n (no LIMIT).",
            "sqlite": "SQLite: strftime('%Y-%m', col), LIMIT n.",
            "oracle": "Oracle: TRUNC(col,'MM'), TO_CHAR(col,'YYYY-MM'), FETCH FIRST n ROWS ONLY.",
        }.get(self.name, "ANSI SQL")


DIALECTS = {
    "postgresql": Dialect("postgresql"),
    "duckdb": Dialect("duckdb"),
    "mysql": Dialect("mysql", "`", "`"),
    "mssql": Dialect("mssql", "[", "]"),
    "sqlite": Dialect("sqlite"),
    "oracle": Dialect("oracle"),
    "generic": Dialect("generic"),
}


def get_dialect(name: str) -> Dialect:
    return DIALECTS.get(name, DIALECTS["generic"])
