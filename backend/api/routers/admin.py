"""Administration: AI providers, users, permissions, usage/cost, audit logs, query history,
jobs, monitoring, data quality, dashboard stats."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from backend.ai.providers import PROVIDER_DEFAULTS, make_provider
from backend.ai.service import PURPOSES, provider_from_config
from backend.api.schemas import AIProviderIn, AIProviderOut, PermissionIn, UserIn, UserOut, UserUpdate
from backend.audit.service import audit
from backend.core.crypto import decrypt, encrypt
from backend.core.db import get_db
from backend.metadata.models import AIProviderConfig, AIUsage, AuditLog, Column, DataQualityIssue, DataSource, Dataset, Insight, Job, Metric, Permission, QueryLog, Relationship, Table, User
from backend.security.auth import get_current_user, hash_password, require_role

router = APIRouter(prefix="/admin", tags=["admin"])


# ------------------------------------------------------------------ AI providers
@router.get("/ai/providers", response_model=list[AIProviderOut])
def list_providers(db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    out = []
    for c in db.scalars(select(AIProviderConfig).order_by(AIProviderConfig.created_at)).all():
        o = AIProviderOut.model_validate(c)
        o.api_key = "********" if c.api_key else None
        out.append(o)
    return out


@router.get("/ai/provider-types")
def provider_types(_: User = Depends(get_current_user)):
    return [{"provider": k, "base_url": v["base_url"], "purposes": PURPOSES} for k, v in PROVIDER_DEFAULTS.items()]


@router.post("/ai/providers", response_model=AIProviderOut, status_code=201)
def create_provider(body: AIProviderIn, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    data = body.model_dump()
    data["api_key"] = encrypt(data["api_key"]) if data.get("api_key") else None
    if data["is_default"]:
        for c in db.scalars(select(AIProviderConfig)).all():
            c.is_default = False
    c = AIProviderConfig(**data)
    db.add(c)
    audit(db, user, "ai_provider.create", "ai_provider", c.id, {"provider": c.provider, "model": c.model})
    db.commit()
    o = AIProviderOut.model_validate(c)
    o.api_key = "********" if c.api_key else None
    return o


@router.put("/ai/providers/{pid}", response_model=AIProviderOut)
def update_provider(pid: str, body: AIProviderIn, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    c = db.get(AIProviderConfig, pid) or _404("Provider")
    data = body.model_dump()
    key = data.pop("api_key")
    if key and key != "********":
        c.api_key = encrypt(key)
    elif key is None:
        c.api_key = None
    if data["is_default"]:
        for other in db.scalars(select(AIProviderConfig)).all():
            other.is_default = False
    for k, v in data.items():
        setattr(c, k, v)
    audit(db, user, "ai_provider.update", "ai_provider", c.id)
    db.commit()
    o = AIProviderOut.model_validate(c)
    o.api_key = "********" if c.api_key else None
    return o


@router.delete("/ai/providers/{pid}", status_code=204)
def delete_provider(pid: str, db: Session = Depends(get_db), _: User = Depends(require_role("admin"))):
    c = db.get(AIProviderConfig, pid) or _404("Provider")
    db.delete(c)
    db.commit()


@router.post("/ai/providers/{pid}/test")
def test_provider(pid: str, db: Session = Depends(get_db), _: User = Depends(require_role("admin"))):
    c = db.get(AIProviderConfig, pid) or _404("Provider")
    res = provider_from_config(c).test()
    if c.embedding_model and res.get("ok"):
        try:
            v = provider_from_config(c).generate_embedding(["test"])
            res["embedding_dims"] = len(v[0])
        except Exception as e:  # noqa: BLE001
            res["embedding_error"] = str(e)[:200]
    return res


@router.post("/ai/providers/test")
def test_provider_new(body: AIProviderIn, _: User = Depends(require_role("admin"))):
    return make_provider(body.provider, body.model, body.api_key, body.base_url, body.temperature, body.max_tokens, body.embedding_model, body.extra).test()


# ------------------------------------------------------------------ users & permissions
@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db), _: User = Depends(require_role("admin"))):
    return db.scalars(select(User).order_by(User.created_at)).all()


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(body: UserIn, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    if db.scalar(select(User).where(User.email == body.email.lower())):
        raise HTTPException(409, "Email already registered")
    u = User(email=body.email.lower(), name=body.name, password_hash=hash_password(body.password), role=body.role, department=body.department)
    db.add(u)
    audit(db, user, "user.create", "user", u.id, {"email": u.email, "role": u.role})
    db.commit()
    return u


@router.patch("/users/{uid}", response_model=UserOut)
def update_user(uid: str, body: UserUpdate, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    u = db.get(User, uid) or _404("User")
    data = body.model_dump(exclude_unset=True)
    if "password" in data:
        u.password_hash = hash_password(data.pop("password"))
    for k, v in data.items():
        setattr(u, k, v)
    audit(db, user, "user.update", "user", u.id, {k: v for k, v in data.items()})
    db.commit()
    return u


@router.get("/permissions")
def list_permissions(db: Session = Depends(get_db), _: User = Depends(require_role("admin"))):
    out = []
    for p in db.scalars(select(Permission).order_by(Permission.role)).all():
        name = None
        if p.resource_type == "dataset":
            name = (db.get(Dataset, p.resource_id) or Dataset(name="?")).name
        elif p.resource_type == "table":
            t = db.get(Table, p.resource_id)
            name = t.qualified_name if t else "?"
        elif p.resource_type == "column":
            c = db.get(Column, p.resource_id)
            name = f"{c.table.qualified_name}.{c.column_name}" if c else "?"
        out.append({"id": p.id, "role": p.role, "resource_type": p.resource_type, "resource_id": p.resource_id, "resource_name": name, "access": p.access, "row_filter": p.row_filter, "mask_columns": p.mask_columns})
    return out


@router.post("/permissions", status_code=201)
def create_permission(body: PermissionIn, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    p = Permission(**body.model_dump())
    db.add(p)
    audit(db, user, "permission.create", "permission", p.id, body.model_dump())
    db.commit()
    return {"id": p.id}


@router.delete("/permissions/{pid}", status_code=204)
def delete_permission(pid: str, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    p = db.get(Permission, pid) or _404("Permission")
    audit(db, user, "permission.delete", "permission", p.id)
    db.delete(p)
    db.commit()


# ------------------------------------------------------------------ usage, audit, queries, jobs
@router.get("/ai/usage")
def ai_usage(days: int = 30, db: Session = Depends(get_db), _: User = Depends(require_role("analyst"))):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.scalars(select(AIUsage).where(AIUsage.created_at >= since)).all()
    by_model: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    for r in rows:
        m = by_model.setdefault(f"{r.provider}/{r.model}", {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0})
        m["requests"] += 1
        m["input_tokens"] += r.input_tokens
        m["output_tokens"] += r.output_tokens
        m["cost"] += r.estimated_cost
        d = by_day.setdefault(r.created_at.strftime("%Y-%m-%d"), {"requests": 0, "tokens": 0, "cost": 0.0})
        d["requests"] += 1
        d["tokens"] += r.input_tokens + r.output_tokens
        d["cost"] += r.estimated_cost
    todays = [r for r in rows if r.created_at.replace(tzinfo=timezone.utc) >= today]
    most_expensive = max(rows, key=lambda r: r.estimated_cost or (r.input_tokens + r.output_tokens), default=None)
    return {
        "today": {"requests": len(todays), "tokens": sum(r.input_tokens + r.output_tokens for r in todays), "cost": round(sum(r.estimated_cost for r in todays), 4)},
        "period": {"requests": len(rows), "tokens": sum(r.input_tokens + r.output_tokens for r in rows), "cost": round(sum(r.estimated_cost for r in rows), 4), "avg_latency_ms": int(sum(r.duration_ms for r in rows) / len(rows)) if rows else 0},
        "by_model": [{"model": k, **v} for k, v in sorted(by_model.items(), key=lambda kv: -kv[1]["requests"])],
        "by_day": [{"day": k, **v} for k, v in sorted(by_day.items())],
        "most_expensive": {"question": most_expensive.question, "model": most_expensive.model, "tokens": most_expensive.input_tokens + most_expensive.output_tokens, "cost": most_expensive.estimated_cost} if most_expensive else None,
        "recent": [{"id": r.id, "created_at": r.created_at, "model": r.model, "purpose": r.purpose, "input_tokens": r.input_tokens, "output_tokens": r.output_tokens, "cost": r.estimated_cost, "duration_ms": r.duration_ms, "question": r.question} for r in sorted(rows, key=lambda r: r.created_at, reverse=True)[:50]],
    }


@router.get("/audit")
def audit_logs(q: str | None = None, action: str | None = None, user_email: str | None = None, limit: int = 200, db: Session = Depends(get_db), _: User = Depends(require_role("admin"))):
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if action:
        stmt = stmt.where(AuditLog.action.like(f"{action}%"))
    if user_email:
        stmt = stmt.where(AuditLog.user_email.ilike(f"%{user_email}%"))
    rows = db.scalars(stmt).all()
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in (r.action + " " + str(r.details) + " " + (r.user_email or "")).lower()]
    return [{"id": r.id, "user_email": r.user_email, "action": r.action, "resource_type": r.resource_type, "resource_id": r.resource_id, "details": r.details, "ip": r.ip, "created_at": r.created_at} for r in rows]


@router.get("/queries")
def query_history(q: str | None = None, status: str | None = None, limit: int = 200, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    stmt = select(QueryLog).order_by(QueryLog.created_at.desc()).limit(limit)
    if user.role != "admin":
        stmt = stmt.where(QueryLog.user_id == user.id)
    if status:
        stmt = stmt.where(QueryLog.status == status)
    if q:
        stmt = stmt.where(QueryLog.sql.ilike(f"%{q}%") | QueryLog.purpose.ilike(f"%{q}%"))
    src = {s.id: s.name for s in db.scalars(select(DataSource)).all()}
    users = {u.id: u.email for u in db.scalars(select(User)).all()}
    return [{"id": r.id, "ref": r.ref, "user": users.get(r.user_id), "source": src.get(r.source_id), "sql": r.sql, "purpose": r.purpose, "status": r.status, "row_count": r.row_count, "duration_ms": r.duration_ms, "error": r.error, "result_preview": r.result_preview, "conversation_id": r.conversation_id, "created_at": r.created_at} for r in db.scalars(stmt).all()]


@router.get("/queries/{ref}")
def query_by_ref(ref: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    r = db.scalar(select(QueryLog).where(QueryLog.ref == ref)) or _404("Query")
    if user.role != "admin" and r.user_id != user.id:
        raise HTTPException(403, "Not your query")
    src = db.get(DataSource, r.source_id) if r.source_id else None
    return {"ref": r.ref, "sql": r.sql, "source": src.name if src else None, "source_id": r.source_id, "purpose": r.purpose, "status": r.status, "row_count": r.row_count, "duration_ms": r.duration_ms, "error": r.error, "result_preview": r.result_preview, "created_at": r.created_at}


@router.get("/jobs")
def list_jobs(limit: int = 50, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    return [{"id": j.id, "kind": j.kind, "target_id": j.target_id, "status": j.status, "progress": j.progress, "message": j.message, "result": j.result, "created_at": j.created_at, "finished_at": j.finished_at} for j in db.scalars(select(Job).order_by(Job.created_at.desc()).limit(limit)).all()]


@router.get("/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    j = db.get(Job, job_id) or _404("Job")
    return {"id": j.id, "kind": j.kind, "target_id": j.target_id, "status": j.status, "progress": j.progress, "message": j.message, "result": j.result, "created_at": j.created_at, "finished_at": j.finished_at}


# ------------------------------------------------------------------ data quality
@router.get("/quality")
def quality(status: str | None = "open", severity: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from backend.security.access import build_access_context

    ctx = build_access_context(db, user)
    stmt = select(DataQualityIssue).options(selectinload(DataQualityIssue.table).selectinload(Table.dataset).selectinload(Dataset.source)).order_by(DataQualityIssue.detected_at.desc())
    if status:
        stmt = stmt.where(DataQualityIssue.status == status)
    if severity:
        stmt = stmt.where(DataQualityIssue.severity == severity)
    out = []
    for i in db.scalars(stmt).all():
        if i.table and not ctx.can_see_table(i.table_id):
            continue
        out.append({"id": i.id, "table_id": i.table_id, "table": i.table.qualified_name if i.table else None, "source": i.table.dataset.source.name if i.table else None, "column_id": i.column_id, "rule": i.rule, "severity": i.severity, "message": i.message, "details": i.details, "status": i.status, "detected_at": i.detected_at, "resolved_at": i.resolved_at})
    return out


@router.patch("/quality/{issue_id}")
def update_issue(issue_id: str, body: dict, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    i = db.get(DataQualityIssue, issue_id) or _404("Issue")
    if body.get("status") in {"open", "acknowledged", "resolved"}:
        i.status = body["status"]
        i.resolved_at = datetime.now(timezone.utc) if i.status == "resolved" else None
    audit(db, user, "quality.update", "quality_issue", i.id, body)
    db.commit()
    return {"ok": True}


@router.post("/quality/run")
def run_quality(db: Session = Depends(get_db), _: User = Depends(require_role("analyst"))):
    from backend.data_quality.engine import run_quality_checks

    n = 0
    for t in db.scalars(select(Table).options(selectinload(Table.columns), selectinload(Table.profiles))).all():
        n += len(run_quality_checks(db, t))
    db.commit()
    return {"issues": n}


# ------------------------------------------------------------------ dashboard & monitoring
@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from backend.metadata.catalog import source_freshness

    since = datetime.now(timezone.utc) - timedelta(days=1)
    srcs = db.scalars(select(DataSource)).all()
    return {
        "sources": {"total": len(srcs), "connected": sum(1 for s in srcs if s.status in {"connected", "imported"}), "error": sum(1 for s in srcs if s.status == "error"), "list": [{"id": s.id, "name": s.name, "type": s.type, "status": s.status, "freshness": source_freshness(s)["label"]} for s in srcs]},
        "tables": db.scalar(select(func.count(Table.id))) or 0,
        "columns": db.scalar(select(func.count(Column.id))) or 0,
        "metrics": db.scalar(select(func.count(Metric.id))) or 0,
        "relationships": {"approved": db.scalar(select(func.count(Relationship.id)).where(Relationship.status == "approved")) or 0, "suggested": db.scalar(select(func.count(Relationship.id)).where(Relationship.status == "suggested")) or 0},
        "quality": {"open": db.scalar(select(func.count(DataQualityIssue.id)).where(DataQualityIssue.status == "open")) or 0, "critical": db.scalar(select(func.count(DataQualityIssue.id)).where(DataQualityIssue.status == "open", DataQualityIssue.severity == "critical")) or 0},
        "insights": [{"id": i.id, "title": i.title, "summary": i.summary, "severity": i.severity, "created_at": i.created_at, "details": {"factors": (i.details or {}).get("factors", [])[:4], "change_pct": (i.details or {}).get("change_pct")}} for i in sorted(db.scalars(select(Insight).where(Insight.status == "new").order_by(Insight.created_at.desc()).limit(50)).all(), key=lambda i: ({"critical": 0, "warning": 1, "info": 2}.get(i.severity, 3), i.kind != "anomaly", -abs((i.details or {}).get("change_pct") or 0)))[:6]],
        "queries_24h": db.scalar(select(func.count(QueryLog.id)).where(QueryLog.created_at >= since)) or 0,
        "ai_requests_24h": db.scalar(select(func.count(AIUsage.id)).where(AIUsage.created_at >= since)) or 0,
        "domains": [{"domain": d or "unassigned", "tables": n} for d, n in db.execute(select(Table.business_domain, func.count(Table.id)).group_by(Table.business_domain)).all()],
    }


@router.get("/monitoring")
def monitoring(db: Session = Depends(get_db), _: User = Depends(require_role("admin"))):
    since = datetime.now(timezone.utc) - timedelta(days=7)
    qs = db.scalars(select(QueryLog).where(QueryLog.created_at >= since)).all()
    ai = db.scalars(select(AIUsage).where(AIUsage.created_at >= since)).all()
    jobs_ = db.scalars(select(Job).where(Job.created_at >= since)).all()
    by_status = {}
    for q in qs:
        by_status[q.status] = by_status.get(q.status, 0) + 1
    durations = sorted(q.duration_ms or 0 for q in qs)
    p = lambda pct: durations[min(len(durations) - 1, int(len(durations) * pct))] if durations else 0  # noqa: E731
    return {
        "queries": {"total": len(qs), "by_status": by_status, "p50_ms": p(0.5), "p95_ms": p(0.95), "errors": [{"ref": q.ref, "error": q.error, "created_at": q.created_at} for q in qs if q.status != "ok"][:20]},
        "ai": {"requests": len(ai), "avg_latency_ms": int(sum(a.duration_ms for a in ai) / len(ai)) if ai else 0, "tokens": sum(a.input_tokens + a.output_tokens for a in ai)},
        "jobs": {"total": len(jobs_), "failed": [{"id": j.id, "kind": j.kind, "message": j.message, "created_at": j.created_at} for j in jobs_ if j.status == "error"][:20], "running": sum(1 for j in jobs_ if j.status in {"queued", "running"})},
        "sources": [{"name": s.name, "status": s.status, "last_error": s.last_error, "health": s.health} for s in db.scalars(select(DataSource)).all()],
    }


def _404(what: str):
    raise HTTPException(404, f"{what} not found")
