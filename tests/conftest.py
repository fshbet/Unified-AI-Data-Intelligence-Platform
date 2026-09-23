"""Shared fixtures: an isolated SQLite metadata catalog per test session, a small synthetic
dataset in CSV/SQLite form, and helpers to run the full pipeline."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="edi_test_"))
os.environ["EDI_DATABASE_URL"] = f"sqlite:///{_TMP / 'meta.db'}"
os.environ["EDI_DATA_DIR"] = str(_TMP / "data")
os.environ["EDI_SCHEDULER_ENABLED"] = "false"

from backend.core.db import Base, SessionLocal, engine  # noqa: E402
from backend.metadata import models  # noqa: E402,F401
from backend.metadata.models import DataSource, Metric, User  # noqa: E402
from backend.security.auth import hash_password  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add(User(email="admin@test", name="Admin", password_hash=hash_password("x"), role="admin"))
        db.add(User(email="analyst@test", name="Analyst", password_hash=hash_password("x"), role="analyst"))
        db.add(User(email="viewer@test", name="Viewer", password_hash=hash_password("x"), role="viewer"))
        db.commit()
    yield


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()


@pytest.fixture
def admin(db):
    return db.query(User).filter_by(role="admin").one()


@pytest.fixture
def viewer(db):
    return db.query(User).filter_by(role="viewer").one()


@pytest.fixture(scope="session")
def demo_sources():
    """Register + fully sync the synthetic Sales/HR/Inventory/Support sources once per session."""
    from backend.workers.jobs import full_sync

    if not (SAMPLES / "sales.csv").exists():
        pytest.skip("run scripts/generate_demo_data.py first")
    with SessionLocal() as db:
        specs = [("Sales", "file", {"files": [str(SAMPLES / "sales.csv"), str(SAMPLES / "products.csv")]}), ("HR", "file", {"files": [str(SAMPLES / "hr.csv")]}), ("Inventory", "file", {"files": [str(SAMPLES / "inventory.csv")]}), ("Support", "file", {"files": [str(SAMPLES / "support.json")]}), ("CRM", "sqlite", {"path": str(SAMPLES / "crm.sqlite")})]
        ids = {}
        for name, typ, cfg in specs:
            s = db.query(DataSource).filter_by(name=name).one_or_none()
            if s is None:
                s = DataSource(name=name, type=typ, config=cfg, department=name)
                db.add(s)
                db.commit()
                full_sync(db, s.id)
            ids[name] = s.id
        db.commit()
        return ids


@pytest.fixture(scope="session")
def demo_metrics(demo_sources):
    from backend.metadata.models import Relationship, Table

    with SessionLocal() as db:
        t = {x.table_name: x for x in db.query(Table).all()}
        cols = {(tb.table_name, c.column_name): c for tb in t.values() for c in tb.columns}
        if not db.query(Metric).filter_by(name="revenue").one_or_none():
            db.add_all([
                Metric(name="revenue", display_name="Revenue", table_id=t["sales"].id, expression="SUM(revenue)", date_column="order_date", unit="INR", format="currency", domain="sales", dimensions=["region", "product_id"]),
                Metric(name="orders", display_name="Orders", table_id=t["sales"].id, expression="COUNT(*)", date_column="order_date", domain="sales"),
                Metric(name="sales_headcount", display_name="Sales Headcount", table_id=t["hr"].id, expression="COUNT(*)", filters="department = 'Sales' AND hire_date < '{period_end}' AND (exit_date IS NULL OR exit_date >= '{period_start}')", domain="hr", dimensions=["region"]),
                Metric(name="product_availability", display_name="Product Availability %", table_id=t["inventory"].id, expression="AVG(availability_pct)", date_column="date", format="percent", domain="inventory", dimensions=["region", "product_id"]),
                Metric(name="support_tickets", display_name="Support Tickets", table_id=t["support"].id, expression="COUNT(*)", date_column="ticket_date", domain="support", direction="down", dimensions=["region"]),
            ])
            for a, b in [(("sales", "employee_id"), ("hr", "employee_id")), (("sales", "product_id"), ("inventory", "product_id")), (("sales", "customer_id"), ("support", "customer_id"))]:
                db.add(Relationship(from_column_id=cols[a].id, to_column_id=cols[b].id, type="many_to_one", confidence=1.0, status="approved", is_cross_source=True))
            db.commit()
        from backend.vector_store.store import reindex_all

        reindex_all(db)
        db.commit()
    return True
