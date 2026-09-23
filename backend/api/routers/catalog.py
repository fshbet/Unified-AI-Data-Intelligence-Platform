"""Datasets, tables, columns: explorer + metadata editing + AI suggestions."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from backend.api.schemas import ColumnOut, ColumnUpdate, DatasetOut, DatasetUpdate, TableOut, TableUpdate
from backend.audit.service import audit
from backend.core.db import get_db
from backend.metadata.catalog import connector_for
from backend.metadata.models import Column, DataQualityIssue, Dataset, Relationship, Table, User
from backend.security.access import build_access_context, mask_result
from backend.security.auth import get_current_user, require_role
from backend.semantic.versioning import record_version, snapshot
from backend.workers import jobs

router = APIRouter(prefix="/catalog", tags=["catalog"])


def _table_out(db: Session, t: Table) -> TableOut:
    o = TableOut.model_validate(t)
    o.source_name, o.source_id, o.dataset_name = t.dataset.source.name, t.dataset.source_id, t.dataset.name
    o.open_issues = db.scalar(select(func.count(DataQualityIssue.id)).where(DataQualityIssue.table_id == t.id, DataQualityIssue.status == "open")) or 0
    col_ids = [c.id for c in t.columns]
    o.relationship_count = db.scalar(select(func.count(Relationship.id)).where(Relationship.status == "approved", (Relationship.from_column_id.in_(col_ids)) | (Relationship.to_column_id.in_(col_ids)))) or 0 if col_ids else 0
    return o


@router.get("/datasets", response_model=list[DatasetOut])
def list_datasets(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ctx = build_access_context(db, user)
    out = []
    for d in db.scalars(select(Dataset).options(selectinload(Dataset.tables), selectinload(Dataset.source))).all():
        visible = [t for t in d.tables if ctx.can_see_table(t.id)]
        if not visible and not ctx.is_admin:
            continue
        o = DatasetOut.model_validate(d)
        o.source_name, o.source_type, o.table_count = d.source.name, d.source.type, len(visible)
        out.append(o)
    return out


@router.patch("/datasets/{dataset_id}", response_model=DatasetOut)
def update_dataset(dataset_id: str, body: DatasetUpdate, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    d = db.get(Dataset, dataset_id) or _404("Dataset")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(d, k, v)
    audit(db, user, "dataset.update", "dataset", d.id, body.model_dump(exclude_unset=True))
    db.commit()
    o = DatasetOut.model_validate(d)
    o.source_name, o.source_type, o.table_count = d.source.name, d.source.type, len(d.tables)
    return o


@router.get("/tables", response_model=list[TableOut])
def list_tables(dataset_id: str | None = None, source_id: str | None = None, domain: str | None = None, q: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ctx = build_access_context(db, user)
    stmt = select(Table).join(Dataset).options(selectinload(Table.dataset).selectinload(Dataset.source), selectinload(Table.columns))
    if dataset_id:
        stmt = stmt.where(Table.dataset_id == dataset_id)
    if source_id:
        stmt = stmt.where(Dataset.source_id == source_id)
    if domain:
        stmt = stmt.where(Table.business_domain == domain)
    if q:
        stmt = stmt.where(Table.table_name.ilike(f"%{q}%") | Table.business_name.ilike(f"%{q}%") | Table.description.ilike(f"%{q}%"))
    return [_table_out(db, t) for t in db.scalars(stmt.order_by(Table.table_name)).all() if ctx.can_see_table(t.id)]


@router.get("/tables/{table_id}")
def get_table(table_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ctx = build_access_context(db, user)
    t = db.get(Table, table_id) or _404("Table")
    if not ctx.can_see_table(t.id):
        raise HTTPException(403, "Access denied")
    cols = [ColumnOut.model_validate(c) for c in ctx.visible_columns(t)]
    col_ids = [c.id for c in t.columns]
    rels = db.scalars(select(Relationship).where((Relationship.from_column_id.in_(col_ids)) | (Relationship.to_column_id.in_(col_ids))).options(selectinload(Relationship.from_column).selectinload(Column.table), selectinload(Relationship.to_column).selectinload(Column.table))).all() if col_ids else []
    issues = db.scalars(select(DataQualityIssue).where(DataQualityIssue.table_id == t.id, DataQualityIssue.status != "resolved")).all()
    from backend.metadata.models import Metric

    metrics = db.scalars(select(Metric).where(Metric.table_id == t.id)).all()
    return {
        "table": _table_out(db, t), "columns": cols,
        "relationships": [{"id": r.id, "from": f"{r.from_column.table.qualified_name}.{r.from_column.column_name}", "to": f"{r.to_column.table.qualified_name}.{r.to_column.column_name}", "type": r.type, "confidence": r.confidence, "status": r.status, "reason": r.reason} for r in rels],
        "issues": [{"id": i.id, "rule": i.rule, "severity": i.severity, "message": i.message, "status": i.status, "detected_at": i.detected_at} for i in issues],
        "metrics": [{"id": m.id, "name": m.name, "display_name": m.display_name, "expression": m.expression} for m in metrics],
        "profiles": [{"row_count": p.row_count, "profiled_at": p.profiled_at, "max_date": p.max_date} for p in sorted(t.profiles, key=lambda p: p.profiled_at)[-20:]],
    }


@router.get("/tables/{table_id}/sample")
def table_sample(table_id: str, n: int = 50, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ctx = build_access_context(db, user)
    t = db.get(Table, table_id) or _404("Table")
    if not ctx.can_see_table(t.id):
        raise HTTPException(403, "Access denied")
    conn = connector_for(t.dataset.source)
    try:
        r = conn.sample_data(t.table_name, t.schema_name, n=min(n, 200))
    finally:
        conn.close()
    denied = {c.column_name for c in t.columns if c.id in ctx.denied_columns}
    keep = [i for i, c in enumerate(r.columns) if c not in denied]
    cols = [r.columns[i] for i in keep]
    rows = [[row[i] for i in keep] for row in r.rows]
    return {"columns": cols, "rows": mask_result(cols, rows, t, ctx)}


@router.patch("/tables/{table_id}", response_model=TableOut)
def update_table(table_id: str, body: TableUpdate, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    t = db.get(Table, table_id) or _404("Table")
    prev = snapshot(t, "table")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(t, k, v)
    record_version(db, "table", t, prev, user.email, t.qualified_name)
    audit(db, user, "table.update", "table", t.id, body.model_dump(exclude_unset=True))
    db.commit()
    return _table_out(db, t)


@router.post("/tables/{table_id}/suggest")
def suggest_table(table_id: str, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    db.get(Table, table_id) or _404("Table")
    return {"job_id": jobs.submit("ai_describe", table_id)}


@router.post("/tables/{table_id}/accept-suggestion")
def accept_table_suggestion(table_id: str, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    from backend.ai.suggestions import apply_suggestion

    t = db.get(Table, table_id) or _404("Table")
    prev = snapshot(t, "table")
    apply_suggestion(db, table=t)
    for c in t.columns:
        if c.ai_suggestion:
            pc = snapshot(c, "column")
            apply_suggestion(db, column=c)
            record_version(db, "column", c, pc, user.email, f"{t.qualified_name}.{c.column_name}")
    record_version(db, "table", t, prev, user.email, t.qualified_name)
    audit(db, user, "table.accept_ai_suggestion", "table", t.id)
    db.commit()
    return {"ok": True}


@router.patch("/columns/{column_id}", response_model=ColumnOut)
def update_column(column_id: str, body: ColumnUpdate, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    c = db.get(Column, column_id) or _404("Column")
    prev = snapshot(c, "column")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(c, k, v)
    if body.sensitivity:
        c.is_sensitive = body.sensitivity != "public"
    record_version(db, "column", c, prev, user.email, f"{c.table.qualified_name}.{c.column_name}")
    audit(db, user, "column.update", "column", c.id, body.model_dump(exclude_unset=True))
    db.commit()
    return ColumnOut.model_validate(c)


@router.post("/columns/{column_id}/accept-suggestion", response_model=ColumnOut)
def accept_column_suggestion(column_id: str, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    from backend.ai.suggestions import apply_suggestion

    c = db.get(Column, column_id) or _404("Column")
    prev = snapshot(c, "column")
    apply_suggestion(db, column=c)
    record_version(db, "column", c, prev, user.email, f"{c.table.qualified_name}.{c.column_name}")
    db.commit()
    return ColumnOut.model_validate(c)


@router.get("/domains")
def domains(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    rows = db.execute(select(Table.business_domain, func.count(Table.id)).group_by(Table.business_domain)).all()
    return [{"domain": d or "unassigned", "tables": n} for d, n in rows]


def _404(what: str):
    raise HTTPException(404, f"{what} not found")
