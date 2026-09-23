"""FastAPI application entry point."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from backend.api.routers import admin, auth, catalog, chat, semantic, sources
from backend.core.config import settings
from backend.core.db import Base, SessionLocal, engine
from backend.metadata import models  # noqa: F401 - register tables
from backend.metadata.models import User
from backend.security.auth import hash_password
from backend.workers.jobs import start_scheduler, stop_scheduler

logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("edi")


def bootstrap() -> None:
    Base.metadata.create_all(engine)  # dev convenience; migrations/ holds the Alembic history for prod
    with SessionLocal() as db:
        if not db.scalar(select(User).limit(1)):
            db.add(User(email=settings.default_admin_email, name="Administrator", password_hash=hash_password(settings.default_admin_password), role="admin"))
            db.commit()
            log.info("created default admin %s", settings.default_admin_email)


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def timing(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Response-Time-Ms"] = str(int((time.perf_counter() - t0) * 1000))
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})


for r in (auth.router, sources.router, catalog.router, semantic.router, chat.router, admin.router):
    app.include_router(r, prefix="/api")


@app.get("/api/health")
def health():
    return {"ok": True, "app": settings.app_name}
