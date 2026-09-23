"""Data quality rules evaluated after every profile run. Issues are upserted by (table, column, rule)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.metadata.models import Column, DataQualityIssue, Relationship, Table, TableProfile

FRESHNESS_DAYS = 3
OPTIONAL_HINTS = ("exit", "end_", "cancel", "resolv", "clos", "termin", "return", "delet", "manager", "parent", "secondary")


def run_quality_checks(db: Session, table: Table) -> list[DataQualityIssue]:
    found: list[dict] = []
    for c in table.columns:
        st = c.stats or {}
        nullp = st.get("null_percentage", 0) or 0
        optional = any(h in c.column_name.lower() for h in OPTIONAL_HINTS)  # e.g. exit_date NULL = still employed
        if nullp >= 20:
            sev = "info" if optional else ("critical" if nullp >= 50 else "warning")
            found.append(dict(column_id=c.id, rule="missing_values", severity=sev, message=f"{table.qualified_name}.{c.column_name} has {nullp:.1f}% missing values" + (" (expected for an optional event column)." if optional else "."), details={"null_percentage": nullp}))
        if c.is_primary_key and st.get("duplicate_count", 0) > 0:
            found.append(dict(column_id=c.id, rule="duplicate_keys", severity="critical", message=f"Primary key {table.qualified_name}.{c.column_name} has {st['duplicate_count']} duplicate values.", details={"duplicates": st["duplicate_count"]}))
        if c.logical_type in {"number", "integer"} and st.get("outlier_count", 0) and st.get("count") and (st.get("distinct_count") or 0) > 12 and st["outlier_count"] / st["count"] > 0.05:
            found.append(dict(column_id=c.id, rule="outliers", severity="info", message=f"{table.qualified_name}.{c.column_name}: {st['outlier_count']} outliers ({100 * st['outlier_count'] / st['count']:.1f}% of rows, IQR rule).", details={"outliers": st["outlier_count"]}))
        if c.logical_type in {"number", "integer"} and st.get("min") is not None and st["min"] < 0 and any(k in c.column_name.lower() for k in ("amount", "revenue", "price", "qty", "quantity", "count", "cost")):
            found.append(dict(column_id=c.id, rule="invalid_values", severity="warning", message=f"{table.qualified_name}.{c.column_name} contains negative values (min {st['min']}).", details={"min": st["min"]}))
        if c.semantic_type == "dimension" and st.get("distinct_count", 0) and st.get("frequency_distribution"):
            vals = [f["value"] for f in st["frequency_distribution"]]
            lowered = {}
            for v in vals:
                lowered.setdefault(v.strip().lower(), []).append(v)
            variants = [vs for vs in lowered.values() if len(vs) > 1]
            if variants:
                found.append(dict(column_id=c.id, rule="inconsistent_categories", severity="warning", message=f"{table.qualified_name}.{c.column_name} has inconsistent spellings: {variants[0]}.", details={"variants": variants[:5]}))
    dup_rows = (table.columns[0].stats or {}).get("duplicate_rows", 0) if table.columns else 0
    if dup_rows and table.row_count and dup_rows / table.row_count > 0.01:
        found.append(dict(column_id=None, rule="duplicate_records", severity="warning", message=f"{table.qualified_name} has {dup_rows} fully duplicated rows.", details={"duplicate_rows": dup_rows}))
    # volume change + schema drift vs previous profile
    profiles = sorted(table.profiles, key=lambda p: p.profiled_at)
    if len(profiles) >= 2:
        prev, cur = profiles[-2], profiles[-1]
        if prev.row_count and cur.row_count is not None:
            change = 100 * (cur.row_count - prev.row_count) / prev.row_count
            if abs(change) >= 30:
                found.append(dict(column_id=None, rule="volume_change", severity="warning", message=f"{table.qualified_name} row count changed {change:+.0f}% since the previous ingestion ({prev.row_count:,} → {cur.row_count:,}). Analysis may be incomplete.", details={"previous": prev.row_count, "current": cur.row_count, "change_pct": round(change, 1)}))
        if prev.column_signature != cur.column_signature:
            found.append(dict(column_id=None, rule="schema_change", severity="warning", message=f"{table.qualified_name} schema changed since the previous sync (columns or types differ).", details={}))
    # freshness: latest date in the primary date column
    if profiles and profiles[-1].max_date:
        try:
            latest = datetime.fromisoformat(profiles[-1].max_date[:19])
            age = (datetime.now() - latest).days
            if age > 365 * 3:
                found.append(dict(column_id=None, rule="stale_data", severity="info", message=f"{table.qualified_name}: newest record is {age} days old ({profiles[-1].max_date[:10]}).", details={"latest": profiles[-1].max_date, "age_days": age}))
        except ValueError:
            pass
    return _upsert(db, table, found)


def check_referential_integrity(db: Session, rel: Relationship, missing_ratio: float) -> DataQualityIssue | None:
    if missing_ratio <= 0.02:
        return None
    t = rel.from_column.table
    found = [dict(column_id=rel.from_column_id, rule="broken_relationship", severity="warning", message=f"{100 * missing_ratio:.1f}% of {t.qualified_name}.{rel.from_column.column_name} values have no match in {rel.to_column.table.qualified_name}.{rel.to_column.column_name}.", details={"missing_ratio": missing_ratio})]
    return _upsert(db, t, found, rules={"broken_relationship"})[0] if found else None


def _upsert(db: Session, table: Table, found: list[dict], rules: set[str] | None = None) -> list[DataQualityIssue]:
    existing = db.scalars(select(DataQualityIssue).where(DataQualityIssue.table_id == table.id, DataQualityIssue.status != "resolved")).all()
    key = lambda i: (i.column_id, i.rule)  # noqa: E731
    by_key = {key(i): i for i in existing}
    out = []
    now = datetime.now(timezone.utc)
    seen = set()
    for f in found:
        k = (f["column_id"], f["rule"])
        seen.add(k)
        if k in by_key:
            i = by_key[k]
            i.message, i.details, i.severity, i.detected_at = f["message"], f["details"], f["severity"], now
        else:
            i = DataQualityIssue(table_id=table.id, **f)
            db.add(i)
        out.append(i)
    for k, i in by_key.items():
        if k in seen:
            continue
        owned = (rules is None and i.rule != "broken_relationship") or (rules is not None and i.rule in rules)
        if owned:
            i.status, i.resolved_at = "resolved", now
    db.flush()
    return out


def issues_for_tables(db: Session, table_ids: list[str] | None) -> list[dict]:
    q = select(DataQualityIssue).where(DataQualityIssue.status == "open").options(selectinload(DataQualityIssue.table))
    if table_ids:
        q = q.where(DataQualityIssue.table_id.in_(table_ids))
    return [{"id": i.id, "table": i.table.qualified_name if i.table else None, "rule": i.rule, "severity": i.severity, "message": i.message, "detected_at": i.detected_at.isoformat()} for i in db.scalars(q).all()]
