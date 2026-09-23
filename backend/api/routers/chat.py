"""AI assistant: conversations + streaming analysis (SSE), ad-hoc SQL, insights."""
from __future__ import annotations

import json
import logging
import queue
import threading

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from sse_starlette.sse import EventSourceResponse

from backend.ai import orchestrator
from backend.api.schemas import AskIn, SQLIn
from backend.audit.service import audit
from backend.core.db import SessionLocal, get_db
from backend.metadata.models import Conversation, DataSource, Insight, Message, User
from backend.query_engine.executor import execute
from backend.security.access import build_access_context
from backend.security.auth import get_current_user, require_role
from backend.workers import jobs

router = APIRouter(prefix="/assistant", tags=["assistant"])
log = logging.getLogger(__name__)

SUGGESTED = [
    "Why did revenue decline in August?",
    "Which region caused the revenue decline?",
    "Is employee attrition affecting sales?",
    "Which customers are driving the decline?",
    "What changed compared with last quarter?",
    "What are the biggest risks right now?",
]


@router.get("/suggestions")
def suggestions(_: User = Depends(get_current_user)):
    return SUGGESTED


@router.get("/conversations")
def list_conversations(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    convs = db.scalars(select(Conversation).where(Conversation.user_id == user.id).order_by(Conversation.updated_at.desc()).limit(50)).all()
    return [{"id": c.id, "title": c.title, "updated_at": c.updated_at, "is_saved": c.is_saved} for c in convs]


@router.get("/conversations/{conv_id}")
def get_conversation(conv_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    c = db.scalar(select(Conversation).where(Conversation.id == conv_id).options(selectinload(Conversation.messages)))
    if not c or (c.user_id != user.id and user.role != "admin"):
        raise HTTPException(404, "Conversation not found")
    return {"id": c.id, "title": c.title, "context": c.context, "is_saved": c.is_saved, "messages": [{"id": m.id, "role": m.role, "content": m.content, "payload": m.payload, "created_at": m.created_at} for m in c.messages]}


@router.patch("/conversations/{conv_id}")
def update_conversation(conv_id: str, body: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    c = db.get(Conversation, conv_id)
    if not c or c.user_id != user.id:
        raise HTTPException(404, "Conversation not found")
    if "title" in body:
        c.title = str(body["title"])[:255]
    if "is_saved" in body:
        c.is_saved = bool(body["is_saved"])
    db.commit()
    return {"ok": True}


@router.delete("/conversations/{conv_id}", status_code=204)
def delete_conversation(conv_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    c = db.get(Conversation, conv_id)
    if c and c.user_id == user.id:
        db.delete(c)
        db.commit()


@router.post("/ask")
def ask(body: AskIn, user: User = Depends(get_current_user)):
    """Server-sent events: status, plan, investigation, tool, token, done, error."""
    q: queue.Queue = queue.Queue()

    def worker() -> None:
        with SessionLocal() as db:
            try:
                u = db.get(User, user.id)
                conv = db.get(Conversation, body.conversation_id) if body.conversation_id else None
                if conv is None or conv.user_id != u.id:
                    conv = Conversation(user_id=u.id)
                    db.add(conv)
                    db.flush()
                db.add(Message(conversation_id=conv.id, role="user", content=body.question))
                audit(db, u, "assistant.ask", "conversation", conv.id, {"question": body.question[:500]})
                db.commit()
                q.put({"type": "conversation", "conversation_id": conv.id})
                for ev in orchestrator.run(db, u, conv, body.question):
                    q.put(ev)
            except Exception as e:  # noqa: BLE001
                log.exception("ask failed")
                q.put({"type": "error", "message": str(e)})
            finally:
                q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def gen():
        while True:
            ev = q.get()
            if ev is None:
                break
            yield {"event": ev["type"], "data": json.dumps(ev, default=str)}

    return EventSourceResponse(gen())


@router.post("/sql")
def run_sql(body: SQLIn, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    src = db.get(DataSource, body.source_id) or HTTPException(404, "Source not found")
    rec = execute(db, build_access_context(db, user), src, body.sql, purpose="manual", limit=min(body.limit, 1000))
    audit(db, user, "sql.execute", "source", src.id, {"ref": rec.ref, "status": "error" if rec.error else "ok"})
    db.commit()
    return rec.to_dict(preview_rows=1000)


# ------------------------------------------------------------------ insights
@router.get("/insights")
def list_insights(status: str | None = None, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    q = select(Insight).order_by(Insight.created_at.desc()).limit(100)
    if status:
        q = q.where(Insight.status == status)
    return [{"id": i.id, "title": i.title, "summary": i.summary, "severity": i.severity, "kind": i.kind, "metric_id": i.metric_id, "period": i.period, "details": i.details, "status": i.status, "created_at": i.created_at} for i in db.scalars(q).all()]


@router.post("/insights/generate")
def gen_insights(_: User = Depends(require_role("analyst"))):
    return {"job_id": jobs.submit("generate_insights")}


@router.patch("/insights/{insight_id}")
def update_insight(insight_id: str, body: dict, db: Session = Depends(get_db), _: User = Depends(get_current_user)):
    i = db.get(Insight, insight_id) or HTTPException(404)
    if body.get("status") in {"new", "investigating", "dismissed"}:
        i.status = body["status"]
    db.commit()
    return {"ok": True}
