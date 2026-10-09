"""Relationship discovery across (and within) sources.

Signals: declared FKs, column-name similarity, matching logical types, cardinality (one side unique),
value overlap measured on distinct-value samples pulled from each source, and existing entity mappings.
Suggestions land with status='suggested'; users approve/reject. Nothing high-impact is auto-approved."""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.metadata.catalog import connector_for
from backend.metadata.models import Column, DataSource, Dataset, Entity, EntityMapping, Relationship, Table

log = logging.getLogger(__name__)

ID_SUFFIXES = ("_id", "_code", "_no", "_number", "_key", "_num", "id", "code")
STOP = {"id", "code", "no", "number", "key", "num", "fk", "pk"}


def key_stem(column_name: str) -> str | None:
    """'customer_id' → 'customer', 'CustomerCode' → 'customer', 'cust_no' → 'cust'. None if not key-like."""
    n = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", column_name).lower()
    for suf in ID_SUFFIXES:
        if n.endswith(suf) and len(n) > len(suf):
            stem = n[: -len(suf)].strip("_")
            return stem or None
    return None


ALIASES = {"cust": "customer", "client": "customer", "emp": "employee", "staff": "employee", "prod": "product", "item": "product", "sku": "product", "acct": "account", "vend": "vendor", "supplier": "vendor", "dept": "department", "org": "organization", "rep": "employee", "salesperson": "employee", "sales_rep": "employee", "owner": "employee"}


def canonical_stem(stem: str) -> str:
    s = stem.strip("_")
    return ALIASES.get(s, s)


def name_similarity(a: str, b: str) -> float:
    a, b = canonical_stem(key_stem(a) or a.lower()), canonical_stem(key_stem(b) or b.lower())
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _distinct_sample(conn, table: Table, col: Column, n: int = 5000) -> set[str]:
    q = f"SELECT DISTINCT {conn.quote_ident(col.column_name)} AS v FROM {conn.quote_table(table.table_name, table.schema_name)} WHERE {conn.quote_ident(col.column_name)} IS NOT NULL"
    r = conn.execute_query(q, limit=n)
    return {_norm(row[0]) for row in r.rows if row[0] is not None}


def _norm(v: Any) -> str:
    s = str(v).strip()
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    return s.lower()


def discover_relationships(db: Session, source_ids: list[str] | None = None, min_confidence: float = 0.5) -> list[Relationship]:
    """Compare every key-like column against every other key-like column in the catalog."""
    q = select(Column).join(Table).join(Dataset).options(selectinload(Column.table).selectinload(Table.dataset))
    if source_ids:
        q = q.where(Dataset.source_id.in_(source_ids))
    cols = [c for c in db.scalars(q).all() if c.semantic_type == "identifier" or key_stem(c.column_name) or c.is_primary_key or c.is_foreign_key]
    all_cols = db.scalars(select(Column).options(selectinload(Column.table).selectinload(Table.dataset))).all()
    key_cols = [c for c in all_cols if c.semantic_type == "identifier" or key_stem(c.column_name) or c.is_primary_key or c.is_foreign_key]
    existing = {(r.from_column_id, r.to_column_id) for r in db.scalars(select(Relationship)).all()}
    existing |= {(b, a) for a, b in existing}

    sample_cache: dict[str, set[str]] = {}
    conn_cache: dict[str, Any] = {}

    def samples(col: Column) -> set[str]:
        if col.id not in sample_cache:
            src = col.table.dataset.source_id
            if src not in conn_cache:
                conn_cache[src] = connector_for(db.get(DataSource, src), db)
            try:
                sample_cache[col.id] = _distinct_sample(conn_cache[src], col.table, col)
            except Exception as e:  # noqa: BLE001
                log.warning("sample failed for %s.%s: %s", col.table.table_name, col.column_name, e)
                sample_cache[col.id] = set()
        return sample_cache[col.id]

    # declared FK targets first
    created: list[Relationship] = []
    by_qualified: dict[str, Column] = {f"{c.table.qualified_name}.{c.column_name}".lower(): c for c in all_cols}
    for c in cols:
        target = (c.stats or {}).get("fk_target")
        if target and (t := by_qualified.get(target.lower())) and (c.id, t.id) not in existing:
            r = Relationship(from_column_id=c.id, to_column_id=t.id, type="many_to_one", confidence=1.0, reason="Declared foreign key constraint", status="suggested", is_cross_source=False, evidence={"signal": "foreign_key"})
            db.add(r)
            created.append(r)
            existing.add((c.id, t.id))
            existing.add((t.id, c.id))

    for a in cols:
        for b in key_cols:
            if a.id == b.id or a.table_id == b.table_id or (a.id, b.id) in existing:
                continue
            if {a.logical_type, b.logical_type} - {"string", "integer", "number"}:
                continue
            sim = name_similarity(a.column_name, b.column_name)
            if sim < 0.6:
                continue
            sa, sb = samples(a), samples(b)
            if not sa or not sb:
                continue
            overlap = len(sa & sb) / max(1, min(len(sa), len(sb)))
            if overlap < 0.2 and sim < 0.95:
                continue
            a_unique = bool((a.stats or {}).get("is_unique"))
            b_unique = bool((b.stats or {}).get("is_unique"))
            # from = many side, to = one side
            if b_unique and not a_unique:
                frm, to, rtype = a, b, "many_to_one"
            elif a_unique and not b_unique:
                frm, to, rtype = b, a, "many_to_one"
            elif a_unique and b_unique:
                frm, to, rtype = a, b, "one_to_one"
            else:
                frm, to, rtype = a, b, "many_to_many"
            confidence = round(min(0.99, 0.45 * sim + 0.5 * overlap + (0.05 if (a_unique or b_unique) else 0)), 3)
            if confidence < min_confidence:
                continue
            cross = a.table.dataset.source_id != b.table.dataset.source_id
            reason = f"Name similarity {sim:.0%}, matching types, {overlap:.1%} of sampled identifiers overlap" + (" (one side unique)" if a_unique or b_unique else "")
            r = Relationship(from_column_id=frm.id, to_column_id=to.id, type=rtype, confidence=confidence, reason=reason, status="suggested", is_cross_source=cross, evidence={"name_similarity": round(sim, 3), "value_overlap": round(overlap, 4), "sample_sizes": [len(sa), len(sb)]})
            db.add(r)
            created.append(r)
            existing.add((a.id, b.id))
            existing.add((b.id, a.id))
    for c in conn_cache.values():
        c.close()
    db.flush()
    return created


def suggest_entities(db: Session) -> list[Entity]:
    """Group identifier columns by canonical stem into canonical entities (Customer, Employee, ...)."""
    cols = db.scalars(select(Column).options(selectinload(Column.table))).all()
    groups: dict[str, list[Column]] = defaultdict(list)
    for c in cols:
        stem = key_stem(c.column_name)
        if stem and (c.semantic_type == "identifier" or c.is_primary_key or c.is_foreign_key):
            groups[canonical_stem(stem)].append(c)
    created: list[Entity] = []
    for stem, members in groups.items():
        if len({m.table_id for m in members}) < 2:
            continue
        name = stem.replace("_", " ").title()
        ent = db.scalar(select(Entity).where(Entity.name == name))
        if ent is None:
            ent = Entity(name=name, description=f"Canonical {name} entity resolved across {len({m.table_id for m in members})} tables.")
            db.add(ent)
            db.flush()
            created.append(ent)
        mapped = {m.column_id for m in ent.mappings}
        for m in members:
            if m.id not in mapped:
                db.add(EntityMapping(entity_id=ent.id, column_id=m.id, role="key", confidence=0.9 if key_stem(m.column_name) == stem else 0.7))
    db.flush()
    return created
