"""FastAPI application entry point."""
from __future__ import annotations

import logging
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from backend.api.routers import admin, auth, catalog, chat, privacy, semantic, sources, unified
from backend.anonymize import LeakError
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
            # A blank setting must never become a blank password. Generate one and print it
            # once — an account nobody can log into is far better than one anybody can.
            password = settings.default_admin_password or secrets.token_urlsafe(18)
            db.add(User(email=settings.default_admin_email, name="Administrator",
                        password_hash=hash_password(password), role="admin"))
            db.commit()
            if settings.default_admin_password:
                log.info("created default admin %s", settings.default_admin_email)
            else:
                log.warning(
                    "Created admin %s with a generated password: %s "
                    "This is shown ONCE. Sign in and change it now.",
                    settings.default_admin_email, password)


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


@app.exception_handler(LeakError)
async def anonymisation_leak(request: Request, exc: LeakError):
    # The message names the values that leaked; it belongs in the log, never in a response body.
    log.critical("egress guard blocked an AI request on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "The request was blocked because unanonymised data was about to be "
                           "sent to the AI provider. Nothing was transmitted. See the server log."},
    )


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    # Never return the exception text. SQLAlchemy and DB-driver errors embed the failing SQL
    # together with its bound parameters — a PendingRollbackError here once returned a full
    # INSERT statement including real row values to the HTTP client.
    reference = secrets.token_hex(8)
    log.exception("unhandled error [%s] on %s", reference, request.url.path)
    return JSONResponse(status_code=500,
                        content={"detail": "Internal server error", "reference": reference})


for r in (auth.router, sources.router, catalog.router, semantic.router, chat.router, admin.router, privacy.router):
    app.include_router(r, prefix="/api")

# The unified output API is the contract external applications attach to. Mounted under
# /api so one origin serves everything, and authenticated by API key rather than a user JWT.
app.include_router(unified.router, prefix="/api")


@app.get("/api/health")
def health():
    return {"ok": True, "app": settings.app_name}
