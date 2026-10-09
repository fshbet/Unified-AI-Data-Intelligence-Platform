"""SQLAlchemy-backed relational connectors: PostgreSQL, MySQL/MariaDB, SQL Server, SQLite, Oracle, DuckDB.
One implementation, per-dialect subclasses only describe how to build the URL."""
from __future__ import annotations

import time
from typing import Any, ClassVar

import pandas as pd
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from backend.connectors.base import ColumnInfo, ConfigField, DataConnector, QueryResult, TableInfo
from backend.connectors.registry import register
from backend.core.config import settings

_INT = ("int", "serial", "bigint", "smallint", "tinyint")
_NUM = ("numeric", "decimal", "float", "double", "real", "money", "number")
_DT = ("timestamp", "datetime")
_D = ("date",)
_B = ("bool", "bit")
_J = ("json", "jsonb")


def logical_type_of(type_name: str) -> str:
    t = type_name.lower()
    if any(k in t for k in _J):
        return "json"
    if any(k in t for k in _B):
        return "boolean"
    if any(k in t for k in _DT):
        return "datetime"
    if any(k in t for k in _D):
        return "date"
    if any(k in t for k in _INT):
        return "integer"
    if any(k in t for k in _NUM):
        return "number"
    return "string"


HOST_FIELDS = [
    ConfigField("host", "Host", default="localhost"),
    ConfigField("port", "Port", type="integer"),
    ConfigField("database", "Database"),
    ConfigField("username", "Username"),
    ConfigField("password", "Password", type="password", required=False),
    ConfigField("schema", "Schema", required=False, help="Restrict discovery to one schema"),
    ConfigField("ssl", "Use SSL", type="boolean", required=False, default=False),
]


class SQLAlchemyConnector(DataConnector):
    default_port: ClassVar[int | None] = None
    config_fields = HOST_FIELDS
    _engine: Engine | None = None

    def build_url(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_engine(self.build_url(), pool_pre_ping=True, future=True, **self.engine_kwargs())
        return self._engine

    def engine_kwargs(self) -> dict[str, Any]:
        return {}

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    def test_connection(self) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            with self.engine.connect() as c:
                c.execute(text("SELECT 1"))
            return {"ok": True, "message": "Connected", "latency_ms": int((time.perf_counter() - t0) * 1000)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}

    def list_schemas(self) -> list[str]:
        if self.config.get("schema"):
            return [self.config["schema"]]
        insp = inspect(self.engine)
        return [s for s in insp.get_schema_names() if s not in {"information_schema", "pg_catalog", "pg_toast", "sys", "performance_schema", "mysql"}]

    def list_tables(self) -> list[TableInfo]:
        insp = inspect(self.engine)
        out: list[TableInfo] = []
        for schema in self.list_schemas() or [None]:
            for name in insp.get_table_names(schema=schema) + insp.get_view_names(schema=schema):
                out.append(TableInfo(name=name, schema=schema))
        return out

    def get_schema(self, table: str, schema: str | None = None) -> TableInfo:
        insp = inspect(self.engine)
        pks = set(insp.get_pk_constraint(table, schema=schema).get("constrained_columns") or [])
        fks: dict[str, str] = {}
        for fk in insp.get_foreign_keys(table, schema=schema):
            for local, remote in zip(fk["constrained_columns"], fk["referred_columns"]):
                fks[local] = f"{fk.get('referred_schema') or schema or ''}.{fk['referred_table']}.{remote}".lstrip(".")
        cols = [
            ColumnInfo(
                name=c["name"],
                data_type=str(c["type"]),
                logical_type=logical_type_of(str(c["type"])),
                nullable=bool(c.get("nullable", True)),
                is_primary_key=c["name"] in pks,
                is_foreign_key=c["name"] in fks,
                fk_target=fks.get(c["name"]),
            )
            for c in insp.get_columns(table, schema=schema)
        ]
        try:
            comment = (insp.get_table_comment(table, schema=schema) or {}).get("text")
        except Exception:  # noqa: BLE001
            comment = None
        return TableInfo(name=table, schema=schema, columns=cols, comment=comment)

    def apply_limit(self, query: str, limit: int) -> str:
        q = query.strip().rstrip(";")
        if " limit " in q.lower() or " top " in q.lower()[:60] or " fetch first " in q.lower():
            return q
        return f"SELECT * FROM ({q}) AS _q LIMIT {limit}"

    def execute_query(self, query: str, limit: int | None = None, timeout: int | None = None) -> QueryResult:
        limit = limit or settings.max_result_rows
        q = self.apply_limit(query, limit + 1)
        t0 = time.perf_counter()
        with self.engine.connect() as c:
            self._set_timeout(c, timeout or settings.query_timeout_seconds)
            df = pd.read_sql_query(text(q), c)
        truncated = len(df) > limit
        return QueryResult.from_df(df.head(limit), duration_ms=int((time.perf_counter() - t0) * 1000), truncated=truncated)

    def _set_timeout(self, conn, seconds: int) -> None:  # noqa: B027
        pass


@register
class PostgresConnector(SQLAlchemyConnector):
    # ambient = peer/IAM auth; service_account = a cloud IAM access token used as the password
    supported_auth = ("credentials", "ambient", "service_account")
    type_key = "postgresql"
    display_name = "PostgreSQL"
    dialect = "postgresql"
    default_port = 5432

    def build_url(self) -> str:
        c = self.config
        ssl = "?sslmode=require" if c.get("ssl") else ""
        return f"postgresql+psycopg2://{c['username']}:{c.get('password', '')}@{c['host']}:{c.get('port') or 5432}/{c['database']}{ssl}"

    def _set_timeout(self, conn, seconds: int) -> None:
        conn.execute(text(f"SET statement_timeout = {seconds * 1000}"))

    def list_schemas(self) -> list[str]:
        return [s for s in super().list_schemas() if s != "information_schema"] or ["public"]


@register
class MySQLConnector(SQLAlchemyConnector):
    supported_auth = ("credentials", "ambient")
    type_key = "mysql"
    display_name = "MySQL / MariaDB"
    dialect = "mysql"
    default_port = 3306

    def build_url(self) -> str:
        c = self.config
        return f"mysql+pymysql://{c['username']}:{c.get('password', '')}@{c['host']}:{c.get('port') or 3306}/{c['database']}"

    def quote_ident(self, name: str) -> str:
        return "`" + name.replace("`", "``") + "`"

    def list_schemas(self) -> list[str]:
        return [self.config.get("schema") or self.config["database"]]

    def _set_timeout(self, conn, seconds: int) -> None:
        conn.execute(text(f"SET SESSION MAX_EXECUTION_TIME={seconds * 1000}"))


@register
class SQLServerConnector(SQLAlchemyConnector):
    # ambient = Windows integrated auth; service_account = an Entra token for Azure SQL
    supported_auth = ("credentials", "ambient", "service_account")
    type_key = "mssql"
    display_name = "Microsoft SQL Server"
    dialect = "mssql"
    default_port = 1433
    config_fields = HOST_FIELDS + [ConfigField("driver", "ODBC Driver", required=False, default="ODBC Driver 18 for SQL Server")]

    def build_url(self) -> str:
        c = self.config
        drv = (c.get("driver") or "ODBC Driver 18 for SQL Server").replace(" ", "+")
        enc = "&Encrypt=yes" if c.get("ssl") else "&Encrypt=no&TrustServerCertificate=yes"
        return f"mssql+pyodbc://{c['username']}:{c.get('password', '')}@{c['host']}:{c.get('port') or 1433}/{c['database']}?driver={drv}{enc}"

    def quote_ident(self, name: str) -> str:
        return "[" + name.replace("]", "]]") + "]"

    def apply_limit(self, query: str, limit: int) -> str:
        q = query.strip().rstrip(";")
        if " top " in q.lower()[:80] or "offset" in q.lower():
            return q
        return f"SELECT TOP {limit} * FROM ({q}) AS _q"


@register
class SQLiteConnector(SQLAlchemyConnector):
    type_key = "sqlite"
    display_name = "SQLite"
    dialect = "sqlite"
    config_fields = [ConfigField("path", "Database file path", help="Absolute path to the .db / .sqlite file")]

    def build_url(self) -> str:
        return f"sqlite:///{self.config['path']}"

    def list_schemas(self) -> list[str]:
        return []

    def list_tables(self) -> list[TableInfo]:
        insp = inspect(self.engine)
        return [TableInfo(name=n) for n in insp.get_table_names() + insp.get_view_names()]


@register
class OracleConnector(SQLAlchemyConnector):
    type_key = "oracle"
    display_name = "Oracle"
    dialect = "oracle"
    default_port = 1521
    config_fields = HOST_FIELDS + [ConfigField("service_name", "Service name", required=False)]

    def build_url(self) -> str:
        c = self.config
        svc = c.get("service_name") or c["database"]
        return f"oracle+oracledb://{c['username']}:{c.get('password', '')}@{c['host']}:{c.get('port') or 1521}/?service_name={svc}"

    def apply_limit(self, query: str, limit: int) -> str:
        q = query.strip().rstrip(";")
        return q if "fetch first" in q.lower() else f"SELECT * FROM ({q}) FETCH FIRST {limit} ROWS ONLY"


@register
class DuckDBFileConnector(SQLAlchemyConnector):
    """Existing DuckDB database file."""

    type_key = "duckdb"
    display_name = "DuckDB"
    dialect = "duckdb"
    category = "database"
    config_fields = [ConfigField("path", "Database file path")]

    def build_url(self) -> str:
        return f"duckdb:///{self.config['path']}"

    @property
    def engine(self) -> Engine:  # duckdb has no SQLAlchemy dialect installed; use native driver
        raise RuntimeError("use native duckdb")

    def _con(self):
        import duckdb

        return duckdb.connect(self.config["path"], read_only=True)

    def test_connection(self) -> dict[str, Any]:
        try:
            self._con().execute("SELECT 1").fetchall()
            return {"ok": True, "message": "Connected"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}

    def list_schemas(self) -> list[str]:
        return ["main"]

    def list_tables(self) -> list[TableInfo]:
        rows = self._con().execute("SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()
        return [TableInfo(name=r[1], schema=None) for r in rows]

    def get_schema(self, table: str, schema: str | None = None) -> TableInfo:
        rows = self._con().execute(f"DESCRIBE {self.quote_table(table)}").fetchall()
        cols = [ColumnInfo(name=r[0], data_type=r[1], logical_type=logical_type_of(r[1]), nullable=(r[2] == "YES")) for r in rows]
        return TableInfo(name=table, columns=cols)

    def execute_query(self, query: str, limit: int | None = None, timeout: int | None = None) -> QueryResult:
        limit = limit or settings.max_result_rows
        t0 = time.perf_counter()
        df = self._con().execute(self.apply_limit(query, limit + 1)).df()
        return QueryResult.from_df(df.head(limit), int((time.perf_counter() - t0) * 1000), truncated=len(df) > limit)
