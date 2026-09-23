"""Data source management: CRUD, test, sync, upload, preview, health."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.api.schemas import SourceIn, SourceOut, SourceUpdate
from backend.audit.service import audit
from backend.connectors.file_connector import SUPPORTED
from backend.connectors.registry import create_connector, list_connector_types
from backend.core.config import settings
from backend.core.crypto import decrypt_config, encrypt_config, mask_config
from backend.core.db import get_db
from backend.metadata.catalog import connector_for, source_freshness
from backend.metadata.models import DataSource, Dataset, Job, Table, User
from backend.security.access import build_access_context
from backend.security.auth import get_current_user, require_role
from backend.workers import jobs

router = APIRouter(prefix="/sources", tags=["sources"])


def _out(db: Session, s: DataSource) -> SourceOut:
    n = db.scalar(select(func.count(Table.id)).join(Dataset).where(Dataset.source_id == s.id)) or 0
    o = SourceOut.model_validate(s)
    o.config = mask_config(dict(s.config or {}))
    o.table_count = n
    o.freshness = source_freshness(s)["label"]
    return o


@router.get("/types")
def connector_types(_: User = Depends(get_current_user)):
    return list_connector_types()


@router.get("", response_model=list[SourceOut])
def list_sources(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return [_out(db, s) for s in db.scalars(select(DataSource).order_by(DataSource.created_at)).all()]


@router.post("", response_model=SourceOut, status_code=201)
def create_source(body: SourceIn, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    if db.scalar(select(DataSource).where(DataSource.name == body.name)):
        raise HTTPException(409, "A source with that name already exists")
    s = DataSource(**body.model_dump(exclude={"config"}), config=encrypt_config(body.config))
    db.add(s)
    audit(db, user, "source.create", "source", s.id, {"name": s.name, "type": s.type})
    db.commit()
    return _out(db, s)


@router.post("/test")
def test_new(body: SourceIn, _: User = Depends(require_role("analyst"))):
    conn = create_connector(body.type, "test", body.config)
    try:
        return conn.test_connection()
    finally:
        conn.close()


@router.get("/{source_id}", response_model=SourceOut)
def get_source(source_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    s = db.get(DataSource, source_id) or _404()
    return _out(db, s)


@router.patch("/{source_id}", response_model=SourceOut)
def update_source(source_id: str, body: SourceUpdate, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    s = db.get(DataSource, source_id) or _404()
    data = body.model_dump(exclude_unset=True)
    if "config" in data:
        merged = decrypt_config(dict(s.config or {}))
        for k, v in data.pop("config").items():
            if v != "********":
                merged[k] = v
        s.config = encrypt_config(merged)
    for k, v in data.items():
        setattr(s, k, v)
    if data.get("is_enabled") is False:
        s.status = "disabled"
    elif data.get("is_enabled") is True and s.status == "disabled":
        s.status = "pending"
    audit(db, user, "source.update", "source", s.id, data)
    db.commit()
    return _out(db, s)


@router.delete("/{source_id}", status_code=204)
def delete_source(source_id: str, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    s = db.get(DataSource, source_id) or _404()
    audit(db, user, "source.delete", "source", s.id, {"name": s.name})
    db.delete(s)
    db.commit()
    for p in (settings.duckdb_dir / f"{source_id}.duckdb", settings.uploads_dir / source_id):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink(missing_ok=True)


@router.post("/{source_id}/test")
def test_source(source_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    s = db.get(DataSource, source_id) or _404()
    conn = connector_for(s)
    try:
        res = conn.test_connection()
    finally:
        conn.close()
    s.health = {**res, "checked_at": datetime.now(timezone.utc).isoformat()}
    if not res.get("ok"):
        s.status, s.last_error = "error", res.get("message")
    elif s.status in {"error", "pending"}:
        s.status = "connected"
        s.last_error = None
    db.commit()
    return res


@router.post("/{source_id}/sync")
def sync(source_id: str, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    s = db.get(DataSource, source_id) or _404()
    audit(db, user, "source.sync", "source", s.id)
    db.commit()
    return {"job_id": jobs.submit("sync_source", s.id)}


@router.post("/{source_id}/upload")
async def upload(source_id: str, files: list[UploadFile] = File(...), db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    s = db.get(DataSource, source_id) or _404()
    if s.type != "file":
        raise HTTPException(400, "Uploads are only supported for file sources")
    d = settings.uploads_dir / source_id
    d.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        if Path(f.filename).suffix.lower() not in SUPPORTED:
            raise HTTPException(400, f"Unsupported file type: {f.filename}")
        target = d / Path(f.filename).name
        with target.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(str(target))
    cfg = decrypt_config(dict(s.config or {}))
    cfg["files"] = sorted(set(cfg.get("files", [])) | set(saved))
    s.config = encrypt_config(cfg)
    audit(db, user, "source.upload", "source", s.id, {"files": [Path(p).name for p in saved]})
    db.commit()
    return {"files": [Path(p).name for p in saved], "job_id": jobs.submit("sync_source", s.id)}


@router.post("/upload-new")
async def upload_new(name: str = Form(...), department: str | None = Form(None), description: str | None = Form(None), files: list[UploadFile] = File(...), db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    """One-shot: create a file source and upload files."""
    if db.scalar(select(DataSource).where(DataSource.name == name)):
        raise HTTPException(409, "A source with that name already exists")
    s = DataSource(name=name, type="file", config={"files": []}, department=department, description=description, owner=user.email)
    db.add(s)
    db.commit()
    return await upload(s.id, files, db, user)


@router.get("/{source_id}/preview")
def preview(source_id: str, table: str, schema: str | None = None, n: int = 50, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    s = db.get(DataSource, source_id) or _404()
    ctx = build_access_context(db, user)
    t = db.scalar(select(Table).join(Dataset).where(Dataset.source_id == s.id, Table.table_name == table))
    if t and not ctx.can_see_table(t.id):
        raise HTTPException(403, "Access denied to this table")
    conn = connector_for(s)
    try:
        r = conn.sample_data(table, schema, n=min(n, 200))
    finally:
        conn.close()
    from backend.security.access import mask_result

    rows = mask_result(r.columns, r.rows, t, ctx) if t else r.rows
    return {"columns": r.columns, "rows": rows, "truncated": r.truncated}


@router.get("/{source_id}/semantic-model")
def semantic_model(source_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    """The source's own semantic model (Power BI / Tableau), as read from the platform."""
    from backend.connectors.bi_base import BIConnector

    s = db.get(DataSource, source_id) or _404()
    conn = connector_for(s)
    try:
        if not isinstance(conn, BIConnector):
            raise HTTPException(400, f"'{s.name}' is not a BI source with a semantic model")
        model, err = conn.semantic_model_safe()
        if model is None:
            raise HTTPException(502, f"Could not read the semantic model: {err}")
        return model.to_dict()
    finally:
        conn.close()


@router.post("/{source_id}/import-semantic-model")
def reimport_semantic_model(source_id: str, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    from backend.connectors.bi_base import BIConnector
    from backend.semantic.bi_import import import_semantic_model

    s = db.get(DataSource, source_id) or _404()
    conn = connector_for(s)
    try:
        if not isinstance(conn, BIConnector):
            raise HTTPException(400, f"'{s.name}' is not a BI source with a semantic model")
        model, err = conn.semantic_model_safe()
        if model is None:
            raise HTTPException(502, f"Could not read the semantic model: {err}")
        out = import_semantic_model(db, s, model, actor=user.email)
        audit(db, user, "source.import_semantic_model", "source", s.id, out)
        db.commit()
        return out
    finally:
        conn.close()


@router.get("/{source_id}/jobs")
def source_jobs(source_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return [{"id": j.id, "kind": j.kind, "status": j.status, "progress": j.progress, "message": j.message, "result": j.result, "created_at": j.created_at, "finished_at": j.finished_at} for j in db.scalars(select(Job).where(Job.target_id == source_id).order_by(Job.created_at.desc()).limit(10)).all()]


def _404():
    raise HTTPException(404, "Data source not found")
