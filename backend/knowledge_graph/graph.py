"""Lightweight knowledge graph over tables, metrics and entities, built from approved relationships
and entity mappings. Used by the query planner to decide which datasets to investigate (multi-hop)."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.metadata.models import Column, Entity, EntityMapping, Metric, Relationship, Table


@dataclass
class Edge:
    src: str  # table id
    dst: str
    via: str  # "col_a -> col_b" or "entity:Customer"
    kind: str  # relationship | entity
    confidence: float
    from_column: str = ""
    to_column: str = ""


@dataclass
class KnowledgeGraph:
    tables: dict[str, Table] = field(default_factory=dict)
    edges: dict[str, list[Edge]] = field(default_factory=lambda: defaultdict(list))
    metrics_by_table: dict[str, list[Metric]] = field(default_factory=lambda: defaultdict(list))

    def neighbors(self, table_id: str) -> list[Edge]:
        return self.edges.get(table_id, [])

    def paths_from(self, table_id: str, max_hops: int = 3) -> dict[str, list[Edge]]:
        """BFS: table_id → {reachable_table_id: [edges along the shortest path]}."""
        seen = {table_id: []}
        q: deque[tuple[str, list[Edge]]] = deque([(table_id, [])])
        while q:
            cur, path = q.popleft()
            if len(path) >= max_hops:
                continue
            for e in self.neighbors(cur):
                if e.dst not in seen:
                    seen[e.dst] = path + [e]
                    q.append((e.dst, path + [e]))
        seen.pop(table_id, None)
        return seen

    def join_key(self, a: str, b: str) -> Edge | None:
        for e in self.neighbors(a):
            if e.dst == b:
                return e
        return None

    def to_dict(self) -> dict:
        nodes = []
        for t in self.tables.values():
            nodes.append({"id": t.id, "label": t.qualified_name, "type": "table", "domain": t.business_domain or (t.dataset.business_domain if t.dataset else None), "source": t.dataset.name if t.dataset else None, "rows": t.row_count, "metrics": [m.name for m in self.metrics_by_table.get(t.id, [])]})
        links, seen = [], set()
        for src, es in self.edges.items():
            for e in es:
                key = tuple(sorted((src, e.dst))) + (e.via,)
                if key in seen:
                    continue
                seen.add(key)
                links.append({"source": src, "target": e.dst, "label": e.via, "kind": e.kind, "confidence": e.confidence})
        return {"nodes": nodes, "links": links}


def build_graph(db: Session, include_suggested: bool = False) -> KnowledgeGraph:
    g = KnowledgeGraph()
    for t in db.scalars(select(Table).options(selectinload(Table.dataset), selectinload(Table.columns))).all():
        g.tables[t.id] = t
    statuses = ["approved"] + (["suggested"] if include_suggested else [])
    rels = db.scalars(select(Relationship).where(Relationship.status.in_(statuses)).options(selectinload(Relationship.from_column).selectinload(Column.table), selectinload(Relationship.to_column).selectinload(Column.table))).all()
    for r in rels:
        a, b = r.from_column.table_id, r.to_column.table_id
        via = f"{r.from_column.column_name} → {r.to_column.column_name}"
        g.edges[a].append(Edge(a, b, via, "relationship", r.confidence, r.from_column.column_name, r.to_column.column_name))
        g.edges[b].append(Edge(b, a, via, "relationship", r.confidence, r.to_column.column_name, r.from_column.column_name))
    for ent in db.scalars(select(Entity).options(selectinload(Entity.mappings).selectinload(EntityMapping.column))).all():
        cols = [m.column for m in ent.mappings]
        for i, ca in enumerate(cols):
            for cb in cols[i + 1 :]:
                if ca.table_id == cb.table_id or g.join_key(ca.table_id, cb.table_id):
                    continue
                g.edges[ca.table_id].append(Edge(ca.table_id, cb.table_id, f"entity:{ent.name}", "entity", 0.9, ca.column_name, cb.column_name))
                g.edges[cb.table_id].append(Edge(cb.table_id, ca.table_id, f"entity:{ent.name}", "entity", 0.9, cb.column_name, ca.column_name))
    for m in db.scalars(select(Metric)).all():
        if m.table_id:
            g.metrics_by_table[m.table_id].append(m)
    return g
