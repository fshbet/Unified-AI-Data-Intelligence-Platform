"""Per-column anonymisation policy, defaulted from the PII classifier the catalog already runs.

The default is *auto-protect detected PII, pass everything else*. A column the profiler flagged
as a name, email, phone, Aadhaar, PAN or salary is protected from first sync without anyone
configuring anything; everything else — crucially including unflagged numeric columns — passes
through so the model can still do arithmetic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.anonymize.strategies import STRATEGIES
from backend.metadata.models import Column, Table

# How the catalog's existing pii_type maps onto a strategy. Reversible pseudonyms for anything
# the answer may need to refer back to; irreversible redaction for values with no analytical
# use whatsoever; generalise/shift where the shape of the value is the useful part.
PII_DEFAULTS: dict[str, tuple[str, str]] = {
    "email": ("pseudonym", "email"),
    "phone": ("pseudonym", "phone"),
    "name": ("pseudonym", "person"),
    "address": ("pseudonym", "address"),
    "employee_id": ("pseudonym", "id"),
    "ip_address": ("pseudonym", "other"),
    "aadhaar": ("redact", "other"),
    "pan": ("redact", "other"),
    "credit_card": ("redact", "other"),
    "bank_account": ("redact", "other"),
    "national_id": ("redact", "other"),
    "date_of_birth": ("shift", "other"),
    "salary": ("generalize", "other"),
}


@dataclass(frozen=True)
class Policy:
    table: str          # qualified table name, lowercased
    column: str         # column name, lowercased
    strategy: str = "pseudonym"
    entity: str = "other"
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy {self.strategy!r}; expected one of {STRATEGIES}")


def default_strategy_for(col: Column) -> tuple[str, str]:
    """(strategy, entity) for a column with no explicit policy."""
    if col.pii_type and col.pii_type in PII_DEFAULTS:
        return PII_DEFAULTS[col.pii_type]
    # `restricted` means the profiler was confident this is sensitive even without a known
    # pii_type. Redact rather than guess at a reversible shape for it.
    if col.sensitivity == "restricted":
        return ("redact", "other")
    return ("passthrough", "other")


def policy_for(col: Column, table_name: str) -> Policy:
    if col.anon_strategy:
        strategy, entity = col.anon_strategy, (col.anon_entity or "other")
    else:
        strategy, entity = default_strategy_for(col)
    return Policy(table_name.lower(), col.column_name.lower(), strategy, entity, col.anon_params or {})


def policies_for_tables(db: Session, table_names: set[str] | None = None) -> dict[tuple[str, str], Policy]:
    """Every effective policy, keyed by (table, column), both lowercased.

    Loads all tables when `table_names` is None: a tool result can carry columns from a join and
    naming which table a value came from is not always possible, so the engine also falls back to
    matching on column name alone (see `Anonymizer._policy`).
    """
    q = select(Table).options(selectinload(Table.columns))
    tables = db.scalars(q).all()
    out: dict[tuple[str, str], Policy] = {}
    for t in tables:
        name = (t.qualified_name or t.table_name or "").lower()
        if table_names and name not in table_names and (t.table_name or "").lower() not in table_names:
            continue
        for col in t.columns:
            p = policy_for(col, name)
            out[(name, p.column)] = p
            # Also index by bare table name so "sales" matches "main.sales".
            short = (t.table_name or "").lower()
            if short and short != name:
                out[(short, p.column)] = p
    return out


def protected_count(policies: dict[tuple[str, str], Policy]) -> int:
    return sum(1 for p in policies.values() if p.strategy != "passthrough")
