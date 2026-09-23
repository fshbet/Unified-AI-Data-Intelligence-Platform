"""Background jobs: source sync (discover → profile → quality → relationships → index), insights,
AI description generation. Runs in a thread pool inside the API process; the same module powers the
separate `worker` container (scheduler loop) in docker-compose.

ponytail: ThreadPoolExecutor + DB job rows. Swap for Celery/RQ when jobs must survive restarts."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from backend.core.config import settings
from backend.core.db import SessionLocal
from backend.data_quality.engine import run_quality_checks
from backend.metadata.catalog import sync_source
from backend.metadata.models import DataSource, Job, Table, User
from backend.semantic.relationships import discover_relationships, suggest_entities
from backend.vector_store.store import reindex_all

log = logging.getLogger(__name__)
_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="job")
FREQ = {"15m": 15, "hourly": 60, "daily": 60 * 24, "weekly": 60 * 24 * 7}


def submit(kind: str, target_id: str | None = None) -> str:
    with SessionLocal() as db:
        job = Job(kind=kind, target_id=target_id)
        db.add(job)
        db.commit()
        job_id = job.id
    _pool.submit(_run, job_id)
    return job_id


def _run(job_id: str) -> None:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        job.status = "running"
        db.commit()
        try:
            if job.kind == "sync_source":
                job.result = full_sync(db, job.target_id, lambda p, m: _progress(db, job, p, m))
            elif job.kind == "generate_insights":
                from backend.analytics.insights import generate_insights

                admin = db.scalar(select(User).where(User.role == "admin"))
                job.result = {"created": len(generate_insights(db, admin))}
            elif job.kind == "reindex":
                from backend.ai.service import embed_fn

                fn, model = embed_fn(db)
                job.result = {"indexed": reindex_all(db, fn, model)}
            elif job.kind == "ai_describe":
                from backend.ai.service import get_provider
                from backend.ai.suggestions import suggest_for_table

                got = get_provider(db, "metadata")
                if not got:
                    raise RuntimeError("No AI provider configured")
                t = db.get(Table, job.target_id)
                job.result = suggest_for_table(db, got[0], t)
            job.status, job.progress = "done", 100
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed", job.kind)
            job.status, job.message = "error", str(e)[:1000]
        job.finished_at = datetime.now(timezone.utc)
        db.commit()


def _progress(db, job: Job, p: int, m: str) -> None:
    job.progress, job.message = p, m
    db.commit()


def full_sync(db, source_id: str, progress=lambda p, m: None) -> dict:
    source = db.get(DataSource, source_id)
    progress(5, "Connecting and discovering schema")
    summary = sync_source(db, source)
    if source.status == "error":
        return summary
    progress(45, "Importing the source's semantic model")
    summary["semantic_model"] = _import_bi_model(db, source)
    progress(55, "Running data-quality checks")
    for ds in source.datasets:
        for t in ds.tables:
            run_quality_checks(db, t)
    db.commit()
    progress(70, "Discovering relationships")
    rels = discover_relationships(db)
    ents = suggest_entities(db)
    db.commit()
    progress(90, "Indexing semantic catalog")
    try:
        from backend.ai.service import embed_fn

        fn, model = embed_fn(db)
        reindex_all(db, fn, model)
    except Exception as e:  # noqa: BLE001
        log.warning("reindex failed: %s", e)
    db.commit()
    summary.update({"relationships_suggested": len(rels), "entities_created": len(ents)})
    return summary


def _import_bi_model(db, source) -> dict | None:
    """Power BI / Tableau ship their own semantic model — import it instead of re-deriving it."""
    from backend.connectors.bi_base import BIConnector
    from backend.metadata.catalog import connector_for
    from backend.semantic.bi_import import import_semantic_model

    conn = connector_for(source)
    if not isinstance(conn, BIConnector):
        return None
    try:
        model, err = conn.semantic_model_safe()
        if model is None:
            log.warning("semantic model unavailable for %s: %s", source.name, err)
            return {"error": err}
        out = import_semantic_model(db, source, model, actor=f"sync:{source.name}")
        db.commit()
        return out
    except Exception as e:  # noqa: BLE001
        log.exception("semantic model import failed for %s", source.name)
        db.rollback()
        return {"error": str(e)[:300]}
    finally:
        conn.close()


# ---------------------------------------------------------------- scheduler
_stop = threading.Event()


def scheduler_loop() -> None:
    while not _stop.is_set():
        try:
            tick()
        except Exception:  # noqa: BLE001
            log.exception("scheduler tick failed")
        _stop.wait(settings.scheduler_tick_seconds)


def tick() -> None:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        for s in db.scalars(select(DataSource).where(DataSource.is_enabled)).all():
            mins = FREQ.get(s.refresh_frequency)
            if not mins:
                continue
            last = s.last_sync_at.replace(tzinfo=timezone.utc) if s.last_sync_at else None
            if last is None or now - last >= timedelta(minutes=mins):
                running = db.scalar(select(Job).where(Job.kind == "sync_source", Job.target_id == s.id, Job.status.in_(["queued", "running"])))
                if not running:
                    log.info("scheduled refresh: %s", s.name)
                    submit("sync_source", s.id)


def start_scheduler() -> None:
    if settings.scheduler_enabled:
        threading.Thread(target=scheduler_loop, daemon=True, name="scheduler").start()


def stop_scheduler() -> None:
    _stop.set()


if __name__ == "__main__":  # standalone worker container
    logging.basicConfig(level=logging.INFO)
    scheduler_loop()
