"""Access control: dataset/table/column permissions, row-level filters and PII masking.

Model: admins see everything. For other roles, a table is visible unless a 'deny' rule exists for
the table or its dataset (default-allow) — OR, when any 'allow' rule exists for the role, only
explicitly allowed datasets/tables are visible (default-deny). Column rules deny individual columns.
Row filters attach a SQL predicate per table for the role. Masking applies to pii/restricted columns
for non-admins unless mask_columns=False rule exists."""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.metadata.models import Column, Permission, Table, User
from backend.metadata.pii import mask_value


@dataclass
class AccessContext:
    user: User
    allowed_tables: set[str]  # table ids
    denied_columns: set[str]  # column ids
    row_filters: dict[str, str]  # table id -> predicate
    unmasked_tables: set[str] = field(default_factory=set)

    @property
    def is_admin(self) -> bool:
        return self.user.role == "admin"

    def can_see_table(self, table_id: str) -> bool:
        return self.is_admin or table_id in self.allowed_tables

    def visible_columns(self, table: Table) -> list[Column]:
        return [c for c in table.columns if self.is_admin or c.id not in self.denied_columns]

    def should_mask(self, table: Table, col: Column) -> bool:
        if self.is_admin or table.id in self.unmasked_tables:
            return False
        return col.sensitivity in {"pii", "restricted"}


def build_access_context(db: Session, user: User) -> AccessContext:
    tables = db.scalars(select(Table).options(selectinload(Table.columns), selectinload(Table.dataset))).all()
    if user.role == "admin":
        return AccessContext(user, {t.id for t in tables}, set(), {}, {t.id for t in tables})
    rules = db.scalars(select(Permission).where(Permission.role == user.role)).all()
    allow_ds = {r.resource_id for r in rules if r.resource_type == "dataset" and r.access == "allow"}
    allow_tb = {r.resource_id for r in rules if r.resource_type == "table" and r.access == "allow"}
    deny_ds = {r.resource_id for r in rules if r.resource_type == "dataset" and r.access == "deny"}
    deny_tb = {r.resource_id for r in rules if r.resource_type == "table" and r.access == "deny"}
    deny_col = {r.resource_id for r in rules if r.resource_type == "column" and r.access == "deny"}
    default_deny = bool(allow_ds or allow_tb)
    allowed: set[str] = set()
    for t in tables:
        if t.id in deny_tb or t.dataset_id in deny_ds:
            continue
        if default_deny and not (t.id in allow_tb or t.dataset_id in allow_ds):
            continue
        allowed.add(t.id)
    # restricted columns are denied for viewers by default
    if user.role == "viewer":
        deny_col |= {c.id for t in tables for c in t.columns if c.sensitivity == "restricted"}
    row_filters = {r.resource_id: r.row_filter for r in rules if r.resource_type == "table" and r.row_filter}
    unmasked = {r.resource_id for r in rules if r.resource_type == "table" and r.mask_columns is False}
    return AccessContext(user, allowed, deny_col, row_filters, unmasked)


def mask_result(columns: list[str], rows: list[list], table: Table, ctx: AccessContext) -> list[list]:
    """Mask PII values in result rows whose column names match sensitive catalog columns."""
    to_mask = {c.column_name: c.pii_type for c in table.columns if ctx.should_mask(table, c)}
    if not to_mask:
        return rows
    idx = [(i, to_mask[name]) for i, name in enumerate(columns) if name in to_mask]
    if not idx:
        return rows
    out = []
    for r in rows:
        r = list(r)
        for i, t in idx:
            r[i] = mask_value(r[i], t)
        out.append(r)
    return out
