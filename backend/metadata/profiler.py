"""Column/table profiling on a (sampled) DataFrame. Produces the stats dict stored on Column.stats."""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from backend.metadata.pii import classify_column

ID_HINTS = ("_id", "_code", "_no", "_number", "_key", "id", "code", "sku")
DATE_HINTS = ("date", "_dt", "_at", "time", "timestamp", "day", "month", "year")


def _py(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return None if math.isnan(float(v)) else round(float(v), 6)
    if isinstance(v, (pd.Timestamp,)):
        return v.isoformat()
    if isinstance(v, (np.bool_,)):
        return bool(v)
    return str(v) if not isinstance(v, (int, str, bool)) else v


def infer_logical_type(s: pd.Series, declared: str = "string") -> str:
    if pd.api.types.is_bool_dtype(s):
        return "boolean"
    if pd.api.types.is_datetime64_any_dtype(s):
        return "datetime"
    if pd.api.types.is_integer_dtype(s):
        return "integer"
    if pd.api.types.is_numeric_dtype(s):
        return "number"
    if declared in {"date", "datetime", "json"}:
        return declared
    if s.dtype == object or pd.api.types.is_string_dtype(s):
        sample = s.dropna().astype(str).head(200)
        if len(sample) and any(h in str(s.name).lower() for h in DATE_HINTS):
            parsed = pd.to_datetime(sample, errors="coerce", format="mixed")
            if parsed.notna().mean() > 0.9:
                return "date" if (parsed.dt.normalize() == parsed).all() else "datetime"
    return "string"


def semantic_type_for(name: str, ltype: str, distinct_ratio: float, n_distinct: int, is_pk: bool) -> str:
    n = name.lower()
    if ltype in {"date", "datetime"}:
        return "date"
    if is_pk or (n.endswith(ID_HINTS) and ltype in {"string", "integer"}) or n in {"id", "key"}:
        return "identifier"
    if ltype in {"number", "integer"}:
        if n_distinct <= 12 and not n.endswith(("amount", "amt", "price", "cost", "qty", "quantity", "count")):
            return "dimension"
        return "measure"
    if ltype == "boolean":
        return "flag"
    if n_distinct <= 50 or distinct_ratio < 0.05:
        return "dimension"
    return "text"


def profile_column(s: pd.Series, declared_type: str = "string", is_pk: bool = False) -> dict[str, Any]:
    n = int(len(s))
    nulls = int(s.isna().sum())
    non_null = s.dropna()
    ltype = infer_logical_type(s, declared_type)
    stats: dict[str, Any] = {
        "count": n,
        "null_count": nulls,
        "null_percentage": round(100 * nulls / n, 3) if n else 0.0,
    }
    try:
        n_distinct = int(non_null.nunique())
    except TypeError:  # unhashable (dict/list cells)
        non_null = non_null.astype(str)
        n_distinct = int(non_null.nunique())
    stats["distinct_count"] = n_distinct
    stats["cardinality"] = round(n_distinct / len(non_null), 4) if len(non_null) else 0.0
    stats["duplicate_count"] = int(len(non_null) - n_distinct)
    stats["is_unique"] = bool(len(non_null) and n_distinct == len(non_null))

    if ltype in {"number", "integer"} and len(non_null):
        v = pd.to_numeric(non_null, errors="coerce").dropna().astype(float)
        if len(v):
            q = v.quantile([0.05, 0.25, 0.5, 0.75, 0.95])
            std = float(v.std()) if len(v) > 1 else 0.0
            iqr = float(q[0.75] - q[0.25])
            lo, hi = q[0.25] - 1.5 * iqr, q[0.75] + 1.5 * iqr
            stats.update(
                min=_py(v.min()), max=_py(v.max()), mean=_py(v.mean()), median=_py(q[0.5]), std=_py(std),
                percentiles={"p5": _py(q[0.05]), "p25": _py(q[0.25]), "p75": _py(q[0.75]), "p95": _py(q[0.95])},
                outlier_count=int(((v < lo) | (v > hi)).sum()) if iqr > 0 else 0,
                sum=_py(v.sum()),
            )
    elif ltype in {"date", "datetime"} and len(non_null):
        d = pd.to_datetime(non_null, errors="coerce", format="mixed").dropna()
        if len(d):
            stats.update(min=d.min().isoformat(), max=d.max().isoformat(), date_range_days=int((d.max() - d.min()).days))
    elif ltype == "string" and len(non_null):
        lens = non_null.astype(str).str.len()
        stats.update(min_length=int(lens.min()), max_length=int(lens.max()), avg_length=round(float(lens.mean()), 2))

    if n_distinct and n_distinct <= 500:
        vc = non_null.astype(str).value_counts().head(20)
        stats["frequency_distribution"] = [{"value": k, "count": int(c)} for k, c in vc.items()]

    samples = non_null.drop_duplicates().head(8).tolist() if n_distinct else []
    pii = classify_column(str(s.name), [str(x) for x in samples])
    return {
        "logical_type": ltype,
        "stats": stats,
        "sample_values": [_py(x) for x in samples],
        "semantic_type": semantic_type_for(str(s.name), ltype, stats["cardinality"], n_distinct, is_pk),
        "pii": pii,
    }


def profile_table(df: pd.DataFrame, declared: dict[str, str] | None = None, pks: set[str] | None = None) -> dict[str, Any]:
    declared, pks = declared or {}, pks or set()
    cols = {c: profile_column(df[c], declared.get(c, "string"), c in pks) for c in df.columns}
    date_cols = [c for c, p in cols.items() if p["logical_type"] in {"date", "datetime"}]
    # best time axis: the date column with the widest range and fewest nulls
    date_column = max(date_cols, key=lambda c: (cols[c]["stats"].get("date_range_days", 0), -cols[c]["stats"]["null_count"])) if date_cols else None
    dup_rows = int(df.duplicated().sum()) if len(df) and len(df.columns) else 0
    return {"row_count": int(len(df)), "column_count": int(len(df.columns)), "columns": cols, "date_column": date_column, "duplicate_rows": dup_rows}
