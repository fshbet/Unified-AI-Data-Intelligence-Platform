"""Semantic layer: metrics, glossary, entities, relationships, versions, graph, search."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.api.schemas import EntityIn, EntityMappingIn, GlossaryIn, GlossaryOut, MetricIn, MetricOut, MetricUpdate, RelationshipIn, RelationshipOut
from backend.audit.service import audit
from backend.core.db import get_db
from backend.knowledge_graph.graph import build_graph
from backend.metadata.models import Column, Entity, EntityMapping, GlossaryTerm, Metric, Relationship, SemanticVersion, Table, User
from backend.security.access import build_access_context
from backend.security.auth import get_current_user, require_role
from backend.semantic.relationships import discover_relationships, suggest_entities
from backend.semantic.versioning import record_version, snapshot
from backend.vector_store.store import CatalogVectorStore, reindex_all

router = APIRouter(prefix="/semantic", tags=["semantic"])


# ------------------------------------------------------------------ metrics
def _metric_out(m: Metric) -> MetricOut:
    o = MetricOut.model_validate(m)
    if m.table:
        o.table_name, o.source_name = m.table.qualified_name, m.table.dataset.source.name
    return o


@router.get("/metrics", response_model=list[MetricOut])
def list_metrics(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return [_metric_out(m) for m in db.scalars(select(Metric).options(selectinload(Metric.table)).order_by(Metric.name)).all()]


@router.post("/metrics", response_model=MetricOut, status_code=201)
def create_metric(body: MetricIn, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    if db.scalar(select(Metric).where(Metric.name == body.name)):
        raise HTTPException(409, "Metric name already exists")
    m = Metric(**body.model_dump())
    db.add(m)
    db.flush()
    record_version(db, "metric", m, None, user.email)
    audit(db, user, "metric.create", "metric", m.id, {"name": m.name})
    _reindex(db)
    db.commit()
    return _metric_out(m)


@router.patch("/metrics/{metric_id}", response_model=MetricOut)
def update_metric(metric_id: str, body: MetricUpdate, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    m = db.get(Metric, metric_id) or _404("Metric")
    prev = snapshot(m, "metric")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(m, k, v)
    record_version(db, "metric", m, prev, user.email)
    audit(db, user, "metric.update", "metric", m.id, body.model_dump(exclude_unset=True))
    _reindex(db)
    db.commit()
    return _metric_out(m)


@router.delete("/metrics/{metric_id}", status_code=204)
def delete_metric(metric_id: str, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    m = db.get(Metric, metric_id) or _404("Metric")
    audit(db, user, "metric.delete", "metric", m.id, {"name": m.name})
    db.delete(m)
    db.commit()


@router.post("/metrics/{metric_id}/preview")
def preview_metric(metric_id: str, period: str | None = None, dimension: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from backend.ai.tools import ToolBox
    from backend.analytics.insights import data_anchor

    m = db.get(Metric, metric_id) or _404("Metric")
    tb = ToolBox(db, build_access_context(db, user), None, data_anchor(db))
    out = tb.t_calculate_metric(m.name, period, dimension)
    db.commit()
    return out


# ------------------------------------------------------------------ glossary
@router.get("/glossary", response_model=list[GlossaryOut])
def list_glossary(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return db.scalars(select(GlossaryTerm).order_by(GlossaryTerm.term)).all()


@router.post("/glossary", response_model=GlossaryOut, status_code=201)
def create_term(body: GlossaryIn, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    if db.scalar(select(GlossaryTerm).where(GlossaryTerm.term == body.term)):
        raise HTTPException(409, "Term already exists")
    g = GlossaryTerm(**body.model_dump())
    db.add(g)
    db.flush()
    record_version(db, "glossary", g, None, user.email)
    _reindex(db)
    db.commit()
    return g


@router.put("/glossary/{term_id}", response_model=GlossaryOut)
def update_term(term_id: str, body: GlossaryIn, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    g = db.get(GlossaryTerm, term_id) or _404("Term")
    prev = snapshot(g, "glossary")
    for k, v in body.model_dump().items():
        setattr(g, k, v)
    record_version(db, "glossary", g, prev, user.email)
    _reindex(db)
    db.commit()
    return g


@router.delete("/glossary/{term_id}", status_code=204)
def delete_term(term_id: str, db: Session = Depends(get_db), _: User = Depends(require_role("admin"))):
    g = db.get(GlossaryTerm, term_id) or _404("Term")
    db.delete(g)
    db.commit()


# ------------------------------------------------------------------ entities
@router.get("/entities")
def list_entities(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    out = []
    for e in db.scalars(select(Entity).options(selectinload(Entity.mappings).selectinload(EntityMapping.column).selectinload(Column.table).selectinload(Table.dataset)).order_by(Entity.name)).all():
        out.append({"id": e.id, "name": e.name, "description": e.description, "domain": e.domain, "mappings": [{"id": m.id, "column_id": m.column_id, "table": m.column.table.qualified_name, "column": m.column.column_name, "source": m.column.table.dataset.source.name, "role": m.role, "confidence": m.confidence} for m in e.mappings]})
    return out


@router.post("/entities", status_code=201)
def create_entity(body: EntityIn, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    e = Entity(**body.model_dump())
    db.add(e)
    db.flush()
    record_version(db, "entity", e, None, user.email)
    db.commit()
    return {"id": e.id}


@router.post("/entities/{entity_id}/mappings", status_code=201)
def add_mapping(entity_id: str, body: EntityMappingIn, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    e = db.get(Entity, entity_id) or _404("Entity")
    db.get(Column, body.column_id) or _404("Column")
    m = EntityMapping(entity_id=e.id, **body.model_dump())
    db.add(m)
    audit(db, user, "entity.map", "entity", e.id, body.model_dump())
    db.commit()
    return {"id": m.id}


@router.delete("/entities/{entity_id}/mappings/{mapping_id}", status_code=204)
def delete_mapping(entity_id: str, mapping_id: str, db: Session = Depends(get_db), _: User = Depends(require_role("analyst"))):
    m = db.get(EntityMapping, mapping_id) or _404("Mapping")
    db.delete(m)
    db.commit()


@router.delete("/entities/{entity_id}", status_code=204)
def delete_entity(entity_id: str, db: Session = Depends(get_db), _: User = Depends(require_role("admin"))):
    e = db.get(Entity, entity_id) or _404("Entity")
    db.delete(e)
    db.commit()


@router.post("/entities/suggest")
def suggest(db: Session = Depends(get_db), _: User = Depends(require_role("analyst"))):
    created = suggest_entities(db)
    db.commit()
    return {"created": [e.name for e in created]}


# ------------------------------------------------------------------ relationships
def _rel_out(r: Relationship) -> RelationshipOut:
    return RelationshipOut(id=r.id, from_column_id=r.from_column_id, to_column_id=r.to_column_id, from_table=r.from_column.table.qualified_name, from_column=r.from_column.column_name, from_source=r.from_column.table.dataset.source.name, to_table=r.to_column.table.qualified_name, to_column=r.to_column.column_name, to_source=r.to_column.table.dataset.source.name, type=r.type, confidence=r.confidence, reason=r.reason, status=r.status, is_cross_source=r.is_cross_source, created_by=r.created_by, evidence=r.evidence or {})


_REL_OPTS = (selectinload(Relationship.from_column).selectinload(Column.table).selectinload(Table.dataset), selectinload(Relationship.to_column).selectinload(Column.table).selectinload(Table.dataset))


@router.get("/relationships", response_model=list[RelationshipOut])
def list_relationships(status: str | None = None, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    q = select(Relationship).options(*_REL_OPTS).order_by(Relationship.confidence.desc())
    if status:
        q = q.where(Relationship.status == status)
    return [_rel_out(r) for r in db.scalars(q).all()]


@router.post("/relationships", response_model=RelationshipOut, status_code=201)
def create_relationship(body: RelationshipIn, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    a, b = db.get(Column, body.from_column_id), db.get(Column, body.to_column_id)
    if not a or not b:
        _404("Column")
    r = Relationship(from_column_id=a.id, to_column_id=b.id, type=body.type, confidence=1.0, reason=body.reason or "Created manually", status="approved", created_by=user.email, is_cross_source=a.table.dataset.source_id != b.table.dataset.source_id)
    db.add(r)
    db.flush()
    record_version(db, "relationship", r, None, user.email, f"{a.table.qualified_name}.{a.column_name}→{b.table.qualified_name}.{b.column_name}")
    audit(db, user, "relationship.create", "relationship", r.id)
    _reindex(db)
    db.commit()
    r = db.scalar(select(Relationship).where(Relationship.id == r.id).options(*_REL_OPTS))
    return _rel_out(r)


@router.post("/relationships/{rel_id}/{decision}", response_model=RelationshipOut)
def decide(rel_id: str, decision: str, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    if decision not in {"approve", "reject"}:
        raise HTTPException(400, "decision must be approve|reject")
    r = db.scalar(select(Relationship).where(Relationship.id == rel_id).options(*_REL_OPTS)) or _404("Relationship")
    prev = snapshot(r, "relationship")
    r.status = "approved" if decision == "approve" else "rejected"
    r.created_by = user.email
    record_version(db, "relationship", r, prev, user.email, f"{r.from_column.table.qualified_name}.{r.from_column.column_name}→{r.to_column.table.qualified_name}.{r.to_column.column_name}")
    audit(db, user, f"relationship.{decision}", "relationship", r.id)
    _reindex(db)
    db.commit()
    return _rel_out(r)


@router.delete("/relationships/{rel_id}", status_code=204)
def delete_relationship(rel_id: str, db: Session = Depends(get_db), _: User = Depends(require_role("admin"))):
    r = db.get(Relationship, rel_id) or _404("Relationship")
    db.delete(r)
    db.commit()


@router.post("/relationships/discover")
def discover(db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    created = discover_relationships(db)
    audit(db, user, "relationship.discover", details={"suggested": len(created)})
    db.commit()
    return {"suggested": len(created)}


@router.get("/graph")
def graph(include_suggested: bool = False, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ctx = build_access_context(db, user)
    g = build_graph(db, include_suggested).to_dict()
    visible = {n["id"] for n in g["nodes"] if ctx.can_see_table(n["id"])}
    return {"nodes": [n for n in g["nodes"] if n["id"] in visible], "links": [l for l in g["links"] if l["source"] in visible and l["target"] in visible]}


# ------------------------------------------------------------------ versions & search
@router.get("/versions")
def versions(object_type: str | None = None, object_id: str | None = None, limit: int = 100, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    q = select(SemanticVersion).order_by(SemanticVersion.created_at.desc()).limit(limit)
    if object_type:
        q = q.where(SemanticVersion.object_type == object_type)
    if object_id:
        q = q.where(SemanticVersion.object_id == object_id)
    return [{"id": v.id, "object_type": v.object_type, "object_id": v.object_id, "object_name": v.object_name, "version": v.version, "changed_by": v.changed_by, "change_summary": v.change_summary, "previous": v.previous, "current": v.current, "created_at": v.created_at} for v in db.scalars(q).all()]


@router.get("/search")
def search(q: str, types: str | None = None, limit: int = 20, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from backend.ai.tools import ToolBox

    tb = ToolBox(db, build_access_context(db, user), None)
    return tb.t_search_metadata(q, limit)


@router.post("/reindex")
def reindex(db: Session = Depends(get_db), _: User = Depends(require_role("analyst"))):
    from backend.ai.service import embed_fn

    fn, model = embed_fn(db)
    n = reindex_all(db, fn, model)
    db.commit()
    return {"indexed": n, "embeddings": model is not None}


def _reindex(db: Session) -> None:
    try:
        reindex_all(db, None, None)
    except Exception:  # noqa: BLE001
        pass


def _404(what: str):
    raise HTTPException(404, f"{what} not found")
