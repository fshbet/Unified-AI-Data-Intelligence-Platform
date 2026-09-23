"""AI-suggested descriptions / semantic types for tables and columns. Suggestions are stored on
`ai_suggestion` for the user to accept or edit — never applied silently."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.ai.providers import AIProvider
from backend.metadata.models import Column, Table

log = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "properties": {
        "table": {"type": "object", "properties": {"business_name": {"type": "string"}, "description": {"type": "string"}, "business_domain": {"type": "string"}}},
        "columns": {"type": "array", "items": {"type": "object", "properties": {"column": {"type": "string"}, "business_name": {"type": "string"}, "description": {"type": "string"}, "semantic_type": {"type": "string", "enum": ["identifier", "measure", "dimension", "date", "text", "flag"]}, "unit": {"type": ["string", "null"]}, "domain": {"type": "string"}}, "required": ["column", "description"]}},
    },
    "required": ["table", "columns"],
}


def suggest_for_table(db: Session, provider: AIProvider, table: Table) -> dict:
    t = db.scalar(select(Table).where(Table.id == table.id).options(selectinload(Table.columns), selectinload(Table.dataset)))
    cols = "\n".join(f"- {c.column_name} ({c.logical_type}, {c.semantic_type}); samples: {c.sample_values[:4]}; distinct={ (c.stats or {}).get('distinct_count') }" for c in t.columns)
    prompt = f"Data source: {t.dataset.name} (domain hint: {t.dataset.business_domain or 'unknown'}).\nTable: {t.qualified_name}, {t.row_count} rows.\nColumns:\n{cols}\n\nWrite concise business descriptions (one sentence each) for the table and every column, a business name, a semantic type, the unit for measures (e.g. INR, days, count) and the business domain (finance, sales, hr, customer, support, inventory, operations, marketing, procurement, product)."
    out = provider.structured_output(SCHEMA, prompt, system="You are a data steward documenting an enterprise data catalog. Be precise and business-oriented.")
    t.ai_suggestion = out.get("table") or {}
    by_name = {c["column"]: c for c in out.get("columns", []) if isinstance(c, dict) and c.get("column")}
    for c in t.columns:
        if c.column_name in by_name:
            c.ai_suggestion = by_name[c.column_name]
    db.flush()
    return out


def apply_suggestion(db: Session, column: Column | None = None, table: Table | None = None) -> None:
    if table and table.ai_suggestion:
        s = table.ai_suggestion
        table.business_name = s.get("business_name") or table.business_name
        table.description = s.get("description") or table.description
        table.business_domain = s.get("business_domain") or table.business_domain
    if column and column.ai_suggestion:
        s = column.ai_suggestion
        column.business_name = s.get("business_name") or column.business_name
        column.description = s.get("description") or column.description
        if s.get("semantic_type"):
            column.semantic_type = s["semantic_type"]
        if s.get("unit"):
            column.unit = s["unit"]
    db.flush()
