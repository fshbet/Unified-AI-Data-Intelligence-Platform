"""Semantic catalog search. Objects (tables, columns, metrics, glossary, relationships) are indexed
as text; when an embedding model is configured, vectors are stored and searched with cosine similarity,
otherwise (or additionally) a lexical BM25-style score is used. Hybrid = max-normalised sum.

ponytail: in-catalog numpy store; implement VectorStore for pgvector/qdrant when > ~100k objects."""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.metadata.models import Column, Embedding, GlossaryTerm, Metric, Relationship, Table

log = logging.getLogger(__name__)
TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    t = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text).lower().replace("_", " ")
    return [w for w in TOKEN.findall(t) if len(w) > 1]


@dataclass
class SearchHit:
    object_type: str
    object_id: str
    text: str
    score: float


class VectorStore(Protocol):
    def upsert(self, object_type: str, object_id: str, text: str, vector: list[float] | None, model: str | None) -> None: ...
    def search(self, query: str, query_vector: list[float] | None, top_k: int, object_types: list[str] | None) -> list[SearchHit]: ...


class CatalogVectorStore:
    def __init__(self, db: Session):
        self.db = db

    def upsert(self, object_type: str, object_id: str, text: str, vector: list[float] | None, model: str | None) -> None:
        e = self.db.scalar(select(Embedding).where(Embedding.object_type == object_type, Embedding.object_id == object_id))
        if e is None:
            e = Embedding(object_type=object_type, object_id=object_id, text=text)
            self.db.add(e)
        e.text = text
        if vector is not None:
            e.vector, e.model = vector, model

    def search(self, query: str, query_vector: list[float] | None = None, top_k: int = 20, object_types: list[str] | None = None) -> list[SearchHit]:
        q = select(Embedding)
        if object_types:
            q = q.where(Embedding.object_type.in_(object_types))
        rows = self.db.scalars(q).all()
        if not rows:
            return []
        lex = _bm25(query, [r.text for r in rows])
        vec = np.zeros(len(rows))
        if query_vector is not None:
            qv = np.array(query_vector, dtype=float)
            for i, r in enumerate(rows):
                if r.vector:
                    v = np.array(r.vector, dtype=float)
                    denom = np.linalg.norm(qv) * np.linalg.norm(v)
                    vec[i] = float(qv @ v / denom) if denom else 0.0
        lex = lex / lex.max() if lex.max() > 0 else lex
        vec = np.clip(vec, 0, None)
        vec = vec / vec.max() if vec.max() > 0 else vec
        score = 0.5 * lex + 0.5 * vec if query_vector is not None else lex
        order = np.argsort(-score)[:top_k]
        return [SearchHit(rows[i].object_type, rows[i].object_id, rows[i].text, float(score[i])) for i in order if score[i] > 0]


def _bm25(query: str, docs: list[str], k1: float = 1.5, b: float = 0.75) -> np.ndarray:
    qt = tokenize(query)
    toks = [tokenize(d) for d in docs]
    n = len(docs)
    avgdl = sum(len(t) for t in toks) / max(1, n)
    df = Counter()
    for t in toks:
        df.update(set(t))
    scores = np.zeros(n)
    for i, t in enumerate(toks):
        tf = Counter(t)
        dl = len(t)
        for w in qt:
            if w not in tf:
                # prefix match helps "revenue" hit "revenue_amt"
                pref = [x for x in tf if x.startswith(w) or w.startswith(x)] if len(w) > 3 else []
                if not pref:
                    continue
                f = sum(tf[x] for x in pref) * 0.6
                d = min(df[x] for x in pref)
            else:
                f, d = tf[w], df[w]
            idf = math.log(1 + (n - d + 0.5) / (d + 0.5))
            scores[i] += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
    return scores


# ---------------------------------------------------------------- indexing text builders
def table_text(t: Table) -> str:
    cols = ", ".join(c.column_name for c in t.columns[:40])
    return f"table {t.qualified_name} ({t.business_name or ''}) domain {t.business_domain or t.dataset.business_domain or ''} source {t.dataset.name}. {t.description or ''} columns: {cols}"


def column_text(c: Column) -> str:
    return f"column {c.table.qualified_name}.{c.column_name} ({c.business_name or ''}) {c.semantic_type or ''} {c.logical_type} domain {c.table.business_domain or ''}. {c.description or ''} {c.business_definition or ''} unit {c.unit or ''}"


def metric_text(m: Metric) -> str:
    return f"metric {m.name} ({m.display_name or ''}) domain {m.domain or ''}: {m.description or ''} formula {m.expression} filters {m.filters or ''} related {' '.join(m.related_metrics or [])}"


def glossary_text(g: GlossaryTerm) -> str:
    return f"glossary term {g.term} domain {g.domain or ''}: {g.definition} synonyms {' '.join(g.synonyms or [])} related {' '.join(g.related_terms or [])}"


def relationship_text(r: Relationship) -> str:
    return f"relationship {r.from_column.table.qualified_name}.{r.from_column.column_name} -> {r.to_column.table.qualified_name}.{r.to_column.column_name} {r.type} {r.reason or ''}"


def reindex_all(db: Session, embed_fn=None, model: str | None = None) -> int:
    """Rebuild the search index. embed_fn(list[str]) -> list[list[float]] is optional."""
    store = CatalogVectorStore(db)
    items: list[tuple[str, str, str]] = []
    for t in db.scalars(select(Table).options(selectinload(Table.columns), selectinload(Table.dataset))).all():
        items.append(("table", t.id, table_text(t)))
        for c in t.columns:
            items.append(("column", c.id, column_text(c)))
    for m in db.scalars(select(Metric)).all():
        items.append(("metric", m.id, metric_text(m)))
    for g in db.scalars(select(GlossaryTerm)).all():
        items.append(("glossary", g.id, glossary_text(g)))
    for r in db.scalars(select(Relationship).where(Relationship.status == "approved").options(selectinload(Relationship.from_column).selectinload(Column.table), selectinload(Relationship.to_column).selectinload(Column.table))).all():
        items.append(("relationship", r.id, relationship_text(r)))
    vectors: list[list[float] | None] = [None] * len(items)
    if embed_fn is not None and items:
        try:
            for i in range(0, len(items), 64):
                batch = [t for _, _, t in items[i : i + 64]]
                vectors[i : i + 64] = embed_fn(batch)
        except Exception as e:  # noqa: BLE001
            log.warning("embedding failed, falling back to lexical only: %s", e)
            vectors = [None] * len(items)
    for (ot, oid, text), vec in zip(items, vectors):
        store.upsert(ot, oid, text, vec, model if vec is not None else None)
    db.flush()
    return len(items)
