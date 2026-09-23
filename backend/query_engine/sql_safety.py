"""Strict read-only SQL validation applied to every query before execution."""
from __future__ import annotations

import re

BLOCKED = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|MERGE|REPLACE|UPSERT|CALL|EXEC|EXECUTE|"
    r"COPY|ATTACH|DETACH|INSTALL|LOAD|PRAGMA|VACUUM|SET|USE|LOCK|UNLOCK|RENAME|COMMENT|SHUTDOWN|INTO\s+OUTFILE|"
    r"xp_cmdshell|sp_executesql|pg_read_file|pg_sleep|load_file|read_csv|read_parquet|read_json|glob|"
    r"httpfs|BENCHMARK|SLEEP|WAITFOR)\b",
    re.IGNORECASE,
)
ALLOWED_START = re.compile(r"^\s*(SELECT|WITH|EXPLAIN|SHOW|DESCRIBE)\b", re.IGNORECASE)


class UnsafeSQL(ValueError):
    pass


def strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", " ", sql)


def _strip_strings(sql: str) -> str:
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def validate_read_only(sql: str) -> str:
    """Return the normalised SQL or raise UnsafeSQL."""
    if not sql or not sql.strip():
        raise UnsafeSQL("Empty query")
    clean = strip_comments(sql).strip().rstrip(";").strip()
    if ";" in _strip_strings(clean):
        raise UnsafeSQL("Multiple statements are not allowed")
    if not ALLOWED_START.match(clean):
        raise UnsafeSQL("Only SELECT / WITH queries are allowed")
    bare = _strip_strings(clean)
    m = BLOCKED.search(bare)
    if m:
        # `SET` / `USE` inside identifiers is fine (e.g. "asset", "user_set") — regex has \b so only bare words hit.
        # CTE names like `WITH created AS` are safe: skip matches that are immediately followed by ' AS'.
        word = m.group(0)
        after = bare[m.end() : m.end() + 4].upper()
        if word.upper() in {"CREATE", "SET", "LOAD", "USE", "CALL"} and after.startswith(" AS"):
            pass
        else:
            raise UnsafeSQL(f"Statement contains blocked keyword '{word.upper()}'")
    return clean


def ensure_limit(sql: str, limit: int, dialect: str = "generic") -> str:
    q = sql.strip().rstrip(";")
    low = q.lower()
    if re.search(r"\blimit\s+\d+", low) or re.search(r"\bfetch\s+first", low) or re.search(r"^\s*select\s+top\s+\d+", low):
        return q
    if dialect == "mssql":
        return f"SELECT TOP {limit} * FROM ({q}) AS _q"
    if dialect == "oracle":
        return f"SELECT * FROM ({q}) FETCH FIRST {limit} ROWS ONLY"
    return f"SELECT * FROM ({q}) AS _q LIMIT {limit}"


def referenced_tables(sql: str) -> set[str]:
    """Best-effort extraction of table identifiers after FROM / JOIN (lower-cased, unquoted)."""
    clean = strip_comments(sql)
    ctes = {m.group(1).lower() for m in re.finditer(r"\b(\w+)\s+AS\s*\(", clean, re.I)}
    names = set()
    for m in re.finditer(r"\b(?:FROM|JOIN)\s+((?:[\"`\[]?[\w]+[\"`\]]?\.)?[\"`\[]?[\w]+[\"`\]]?)", clean, re.I):
        n = re.sub(r"[\"`\[\]]", "", m.group(1)).lower()
        if n not in ctes and not n.startswith("("):
            names.add(n)
    return names
