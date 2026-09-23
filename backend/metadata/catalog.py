"""Catalog service: connects a DataSource, discovers schema, profiles, classifies PII and
persists everything to the metadata repository. Also the single place that builds connectors
from stored (encrypted) config."""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.connectors.base import DataConnector, TableInfo
from backend.connectors.registry import create_connector
from backend.core.config import settings
from backend.core.crypto import decrypt_config
from backend.metadata.models import Column, DataSource, Dataset, Table, TableProfile
from backend.metadata.profiler import profile_table

log = logging.getLogger(__name__)

DOMAIN_HINTS = {
    "finance": ("invoice", "revenue", "ledger", "gl_", "finance", "expense", "cost", "budget", "payment", "ar_", "ap_"),
    "sales": ("sales", "order", "deal", "opportunit", "pipeline", "quote"),
    "hr": ("employee", "hr", "staff", "payroll", "attendance", "recruit", "attrition", "headcount", "leave"),
    "customer": ("customer", "client", "crm", "account", "contact"),
    "support": ("ticket", "support", "complaint", "case", "sla", "csat"),
    "inventory": ("inventory", "stock", "warehouse", "sku"),
    "operations": ("production", "delivery", "shipment", "logistic", "operation", "plant"),
    "procurement": ("vendor", "supplier", "purchase", "procure", "po_"),
    "marketing": ("campaign", "marketing", "lead", "ad_", "spend", "impression"),
    "product": ("product", "catalog", "item"),
    "projects": ("project", "task", "milestone"),
}


def infer_domain(*names: str | None) -> str | None:
    text = " ".join(n.lower() for n in names if n)
    for domain, hints in DOMAIN_HINTS.items():
        if any(h in text for h in hints):
            return domain
    return None


def humanize(name: str) -> str:
    n = re.sub(r"[_\-]+", " ", name).strip()
    n = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", n)
    words = n.split()
    fixes = {"id": "ID", "amt": "Amount", "qty": "Quantity", "dt": "Date", "no": "Number", "emp": "Employee", "cust": "Customer", "num": "Number", "pct": "Percent", "avg": "Average"}
    return " ".join(fixes.get(w.lower(), w.capitalize()) for w in words)


def connector_for(source: DataSource) -> DataConnector:
    return create_connector(source.type, source.id, decrypt_config(dict(source.config or {})))


def sync_source(db: Session, source: DataSource, profile: bool = True, sample_rows: int | None = None) -> dict[str, Any]:
    """Discover + profile + persist. Returns summary. Idempotent (upserts by table name)."""
    conn = connector_for(source)
    summary: dict[str, Any] = {"tables": 0, "columns": 0, "errors": []}
    try:
        refresh_info = conn.refresh()
        summary["refresh"] = refresh_info
        dataset = _ensure_dataset(db, source)
        existing = {t.qualified_name: t for t in dataset.tables}
        seen: set[str] = set()
        for tinfo in conn.list_tables():
            try:
                full = conn.get_schema(tinfo.name, tinfo.schema)
                full.row_count = tinfo.row_count
                table = _upsert_table(db, dataset, full, existing)
                seen.add(table.qualified_name)
                if profile:
                    _profile_table(db, conn, table, full, sample_rows or settings.profile_sample_rows)
                summary["tables"] += 1
                summary["columns"] += len(full.columns)
            except Exception as e:  # noqa: BLE001
                log.exception("failed to sync table %s", tinfo.name)
                summary["errors"].append({"table": tinfo.name, "error": str(e)})
        for name, t in existing.items():  # tables that disappeared
            if name not in seen:
                t.tags = list({*(t.tags or []), "missing"})
        source.status = "imported" if conn.category in {"file", "api", "nosql"} else "connected"
        source.last_sync_at = datetime.now(timezone.utc)
        source.last_error = None
        source.health = {"ok": True, "checked_at": source.last_sync_at.isoformat(), "tables": summary["tables"]}
        db.commit()
    except Exception as e:  # noqa: BLE001
        log.exception("sync failed for %s", source.name)
        source.status = "error"
        source.last_error = str(e)
        source.health = {"ok": False, "checked_at": datetime.now(timezone.utc).isoformat(), "error": str(e)}
        db.commit()
        summary["errors"].append({"source": source.name, "error": str(e)})
    finally:
        conn.close()
    return summary


def _ensure_dataset(db: Session, source: DataSource) -> Dataset:
    ds = db.scalar(select(Dataset).where(Dataset.source_id == source.id))
    if ds is None:
        ds = Dataset(source_id=source.id, name=source.name, description=source.description, owner=source.owner,
                     business_domain=infer_domain(source.name, source.department, source.description) or (source.department or "").lower() or None,
                     refresh_frequency=source.refresh_frequency)
        db.add(ds)
        db.flush()
    return ds


def _upsert_table(db: Session, dataset: Dataset, info: TableInfo, existing: dict[str, Table]) -> Table:
    table = existing.get(info.qualified)
    if table is None:
        table = Table(dataset_id=dataset.id, schema_name=info.schema, table_name=info.name,
                      business_name=humanize(info.name), description=info.comment,
                      business_domain=infer_domain(info.name, dataset.name) or dataset.business_domain)
        db.add(table)
        db.flush()
        existing[info.qualified] = table
    table.row_count = info.row_count if info.row_count is not None else table.row_count
    table.column_count = len(info.columns)
    table.last_updated = datetime.now(timezone.utc)
    by_name = {c.column_name: c for c in table.columns}
    for i, ci in enumerate(info.columns):
        col = by_name.get(ci.name)
        if col is None:
            col = Column(table_id=table.id, column_name=ci.name, business_name=humanize(ci.name), data_type=ci.data_type)
            db.add(col)
            table.columns.append(col)
        col.ordinal = i
        col.data_type = ci.data_type
        col.logical_type = ci.logical_type
        col.is_nullable = ci.nullable
        col.is_primary_key = ci.is_primary_key or col.is_primary_key
        col.is_foreign_key = ci.is_foreign_key or col.is_foreign_key
        if ci.fk_target:
            col.stats = {**(col.stats or {}), "fk_target": ci.fk_target}
    incoming = {c.name for c in info.columns}
    for name, col in by_name.items():
        if name not in incoming:
            db.delete(col)
    db.flush()
    return table


def _profile_table(db: Session, conn: DataConnector, table: Table, info: TableInfo, sample_rows: int) -> None:
    df = conn.get_statistics(info.name, info.schema, sample_rows=sample_rows)
    declared = {c.name: c.logical_type for c in info.columns}
    pks = {c.name for c in info.columns if c.is_primary_key}
    prof = profile_table(df, declared, pks)
    if info.row_count is None:
        try:
            table.row_count = conn.get_row_count(info.name, info.schema)
        except Exception:  # noqa: BLE001
            table.row_count = prof["row_count"]
    table.date_column = prof["date_column"]
    table.last_profiled_at = datetime.now(timezone.utc)
    max_date = None
    for col in table.columns:
        p = prof["columns"].get(col.column_name)
        if not p:
            continue
        col.logical_type = p["logical_type"]
        col.stats = {**p["stats"], "duplicate_rows": prof["duplicate_rows"]}
        col.sample_values = p["sample_values"]
        col.semantic_type = col.semantic_type if col.semantic_type in {"measure", "dimension", "identifier", "date"} and col.business_definition else p["semantic_type"]
        if p["stats"].get("is_unique") and col.semantic_type == "identifier" and not pks:
            col.is_primary_key = col.is_primary_key or (p["stats"]["null_count"] == 0)
        pii = p["pii"]
        if pii["pii_type"]:
            col.pii_type = pii["pii_type"]
            col.sensitivity = pii["sensitivity"]
            col.is_sensitive = True
        if col.column_name == table.date_column:
            max_date = p["stats"].get("max")
        if not col.description:
            col.description = _heuristic_description(table, col)
    sig = hashlib.md5(",".join(f"{c.column_name}:{c.logical_type}" for c in table.columns).encode()).hexdigest()
    db.add(TableProfile(table_id=table.id, row_count=table.row_count or prof["row_count"], column_signature=sig, max_date=max_date))
    db.flush()


def _heuristic_description(table: Table, col: Column) -> str | None:
    n = col.column_name.lower()
    st = col.semantic_type
    entity = humanize(table.table_name).rstrip("s")
    if st == "identifier":
        return f"Identifier for {humanize(n.replace('_id', '').replace('_code', '')) or entity}."
    if st == "date":
        return f"{humanize(n)} timestamp for each {entity} record."
    if st == "measure":
        return f"Numeric measure: {humanize(n)}."
    if st == "dimension":
        return f"Categorical attribute {humanize(n)} used to segment {humanize(table.table_name)}."
    return None


def source_freshness(source: DataSource) -> dict[str, Any]:
    if not source.last_sync_at:
        return {"age_minutes": None, "label": "never synced"}
    age = (datetime.now(timezone.utc) - source.last_sync_at.replace(tzinfo=timezone.utc)).total_seconds() / 60
    if age < 60:
        label = f"{int(age)} min ago"
    elif age < 60 * 24:
        label = f"{int(age // 60)} h ago"
    else:
        label = f"{int(age // (60 * 24))} d ago"
    return {"age_minutes": int(age), "label": label}
