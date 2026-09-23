"""Seed the demo environment: register the synthetic sources, sync + profile them, define governed
metrics / glossary / entities, approve high-confidence relationships, configure a local Ollama
provider (if reachable), create demo users + permissions and generate proactive insights.

    python scripts/seed_demo.py [--reset]
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import httpx
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.config import settings  # noqa: E402
from backend.core.crypto import encrypt  # noqa: E402
from backend.core.db import SessionLocal  # noqa: E402
from backend.main import bootstrap  # noqa: E402
from backend.metadata.models import AIProviderConfig, Column, DataSource, Entity, EntityMapping, GlossaryTerm, Metric, Permission, Relationship, Table, User  # noqa: E402
from backend.security.auth import hash_password  # noqa: E402
from backend.semantic.versioning import record_version  # noqa: E402
from backend.vector_store.store import reindex_all  # noqa: E402
from backend.workers.jobs import full_sync  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("seed")
S = (Path(__file__).resolve().parents[1] / "data" / "samples").resolve()

SOURCES = [
    dict(name="Sales", type="file", config={"files": [str(S / "sales.csv"), str(S / "products.csv")]}, department="Sales", owner="sales.ops@example.com", description="Order-level sales transactions and product master exported from the order management system.", refresh_frequency="daily"),
    dict(name="Finance", type="file", config={"files": [str(S / "finance.xlsx")]}, department="Finance", owner="finance@example.com", description="Invoices (posted/cancelled) with cost and departmental budgets from the finance ERP.", refresh_frequency="daily"),
    dict(name="HR", type="file", config={"files": [str(S / "hr.csv")]}, department="HR", owner="hr@example.com", description="Employee master: department, region, hire and exit dates, compensation.", refresh_frequency="daily"),
    dict(name="Inventory", type="file", config={"files": [str(S / "inventory.csv")]}, department="Operations", owner="ops@example.com", description="Daily stock levels and product availability by region.", refresh_frequency="hourly"),
    dict(name="Support", type="file", config={"files": [str(S / "support.json")]}, department="Customer Support", owner="support@example.com", description="Customer support tickets exported from the helpdesk API (JSON).", refresh_frequency="hourly"),
    dict(name="Marketing", type="file", config={"files": [str(S / "marketing.csv")]}, department="Marketing", owner="marketing@example.com", description="Monthly campaign spend, impressions, leads and conversions by region and channel.", refresh_frequency="weekly"),
    dict(name="CRM (SQLite)", type="sqlite", config={"path": str(S / "crm.sqlite")}, department="Sales", owner="crm.admin@example.com", description="CRM database: accounts, contacts and product catalog. Uses customer_code / sku identifiers.", refresh_frequency="hourly"),
]

METRICS = [
    dict(name="revenue", display_name="Revenue", description="Total order revenue after discounts, recognised on order date (Sales system view).", table="sales", expression="SUM(revenue)", date_column="order_date", unit="INR", format="currency", domain="sales", owner="Finance", direction="up", dimensions=["region", "product_id", "channel", "discount_pct", "customer_id", "employee_id"], related_metrics=["orders", "posted_revenue", "average_order_value", "gross_margin"]),
    dict(name="orders", display_name="Orders", description="Number of orders placed.", table="sales", expression="COUNT(*)", date_column="order_date", unit="orders", domain="sales", owner="Sales Ops", dimensions=["region", "product_id", "channel"], related_metrics=["revenue", "average_order_value"]),
    dict(name="average_order_value", display_name="Average Order Value", description="Mean revenue per order.", table="sales", expression="AVG(revenue)", date_column="order_date", unit="INR", format="currency", domain="sales", owner="Sales Ops", dimensions=["region", "product_id"], related_metrics=["revenue", "orders"]),
    dict(name="average_discount", display_name="Average Discount %", description="Mean discount percentage applied to orders.", table="sales", expression="AVG(discount_pct)", date_column="order_date", unit="%", format="percent", domain="sales", owner="Sales Ops", direction="down", dimensions=["region", "channel"]),
    dict(name="delivery_delay_rate", display_name="Delivery Delay Rate", description="Share of orders delivered later than 7 days.", table="sales", expression="100.0 * AVG(delivery_delayed)", date_column="order_date", unit="%", format="percent", domain="operations", owner="Operations", direction="down", dimensions=["region", "product_id"]),
    dict(name="average_delivery_days", display_name="Average Delivery Days", description="Mean days from order to delivery.", table="sales", expression="AVG(delivery_days)", date_column="order_date", unit="days", domain="operations", owner="Operations", direction="down", dimensions=["region"]),
    dict(name="posted_revenue", display_name="Posted Revenue (Finance)", description="Official revenue: sum of invoice amounts where invoice status is POSTED. Cancelled invoices excluded.", table="finance_invoices", expression="SUM(invoice_amount)", filters="invoice_status = 'POSTED'", date_column="invoice_date", unit="INR", format="currency", domain="finance", owner="Finance", dimensions=["region"], related_metrics=["revenue", "gross_margin"]),
    dict(name="gross_margin", display_name="Gross Margin", description="Posted invoice amount minus cost of goods.", table="finance_invoices", expression="SUM(invoice_amount - cost_amount)", filters="invoice_status = 'POSTED'", date_column="invoice_date", unit="INR", format="currency", domain="finance", owner="Finance", dimensions=["region"], related_metrics=["posted_revenue"]),
    dict(name="cancelled_invoices", display_name="Cancelled Invoices", description="Count of invoices with status CANCELLED.", table="finance_invoices", expression="COUNT(*)", filters="invoice_status = 'CANCELLED'", date_column="invoice_date", unit="invoices", domain="finance", owner="Finance", direction="down", dimensions=["region"]),
    dict(name="sales_headcount", display_name="Sales Headcount", description="Employees in the Sales department active at any point during the period (hired before period end and not exited before period start).", table="hr", expression="COUNT(*)", filters="department = 'Sales' AND hire_date < '{period_end}' AND (exit_date IS NULL OR exit_date >= '{period_start}')", unit="employees", domain="hr", owner="HR", dimensions=["region"], related_metrics=["employee_exits", "attrition_rate"]),
    dict(name="employee_exits", display_name="Employee Exits", description="Employees whose exit date falls in the period.", table="hr", expression="COUNT(*)", date_column="exit_date", unit="employees", domain="hr", owner="HR", direction="down", dimensions=["department", "region"], related_metrics=["sales_headcount"]),
    dict(name="new_hires", display_name="New Hires", description="Employees whose hire date falls in the period.", table="hr", expression="COUNT(*)", date_column="hire_date", unit="employees", domain="hr", owner="HR", dimensions=["department", "region"]),
    dict(name="product_availability", display_name="Product Availability %", description="Average product availability across daily inventory snapshots.", table="inventory", expression="AVG(availability_pct)", date_column="date", unit="%", format="percent", domain="inventory", owner="Operations", dimensions=["region", "product_id"], related_metrics=["stock_units"]),
    dict(name="stock_units", display_name="Average Stock Units", description="Average units in stock across snapshots.", table="inventory", expression="AVG(stock_units)", date_column="date", unit="units", domain="inventory", owner="Operations", dimensions=["region", "product_id"]),
    dict(name="support_tickets", display_name="Support Tickets", description="Number of support tickets opened.", table="support", expression="COUNT(*)", date_column="ticket_date", unit="tickets", domain="support", owner="Customer Support", direction="down", dimensions=["region", "issue_type", "priority"], related_metrics=["sla_breach_rate", "csat"]),
    dict(name="sla_breach_rate", display_name="SLA Breach Rate", description="Share of tickets that breached the 48h resolution SLA.", table="support", expression="100.0 * AVG(CASE WHEN sla_breached THEN 1 ELSE 0 END)", date_column="ticket_date", unit="%", format="percent", domain="support", owner="Customer Support", direction="down", dimensions=["region", "issue_type"]),
    dict(name="csat", display_name="Customer Satisfaction (CSAT)", description="Mean CSAT score (1-5) on closed tickets.", table="support", expression="AVG(csat)", date_column="ticket_date", unit="score", domain="support", owner="Customer Support", dimensions=["region", "issue_type"]),
    dict(name="marketing_spend", display_name="Marketing Spend", description="Total campaign spend.", table="marketing", expression="SUM(spend)", date_column="month", unit="INR", format="currency", domain="marketing", owner="Marketing", direction="down", dimensions=["region", "channel"], related_metrics=["leads"]),
    dict(name="leads", display_name="Marketing Leads", description="Leads generated by campaigns.", table="marketing", expression="SUM(leads)", date_column="month", unit="leads", domain="marketing", owner="Marketing", dimensions=["region", "channel"], related_metrics=["marketing_spend"]),
]

GLOSSARY = [
    dict(term="Revenue", definition="Recognised revenue from customer orders, net of discounts. The Finance definition (posted invoices only) is the official figure for reporting; the Sales system figure includes orders not yet invoiced.", domain="finance", owner="Finance", synonyms=["sales revenue", "turnover", "top line"], related_terms=["Gross Margin", "Orders", "Posted Revenue"], rules=["Official revenue = SUM(invoice_amount) WHERE invoice_status = 'POSTED'", "Cancelled invoices are excluded", "Currency: INR"]),
    dict(term="Attrition", definition="Employees leaving the organisation. Attrition rate = exits in period / headcount at start of period.", domain="hr", owner="HR", synonyms=["employee turnover", "churn (employees)", "separations"], related_terms=["Headcount", "Employee Exits"], rules=["exit_date not null and within the period"]),
    dict(term="Headcount", definition="Number of employees active during a period: hired before the period end and not exited before the period start.", domain="hr", owner="HR", synonyms=["workforce size", "staff count"], related_terms=["Attrition"]),
    dict(term="Product Availability", definition="Percentage of days a product is in stock and orderable in a region, based on daily inventory snapshots.", domain="inventory", owner="Operations", synonyms=["stock availability", "in-stock rate"], related_terms=["Stock Units"]),
    dict(term="Delivery Delay", definition="An order delivered more than 7 days after the order date.", domain="operations", owner="Operations", synonyms=["late delivery"], related_terms=["Delivery Delay Rate", "Support Tickets"]),
    dict(term="Customer", definition="A billing account that places orders. Identified as customer_id in Sales/Finance/Support and customer_code in the CRM.", domain="customer", owner="Sales Ops", synonyms=["account", "client"], related_terms=["Orders", "Support Tickets"]),
    dict(term="Region", definition="Sales territory: North, South, East, West. Consistent across all systems.", domain="sales", owner="Sales Ops", synonyms=["territory", "zone"]),
    dict(term="Gross Margin", definition="Posted invoice amount minus cost of goods sold.", domain="finance", owner="Finance", synonyms=["GM", "gross profit"], related_terms=["Revenue"]),
]

ENTITIES = {
    "Customer": [("sales", "customer_id"), ("finance_invoices", "customer_id"), ("support", "customer_id"), ("customers", "customer_id"), ("accounts", "customer_code"), ("contacts", "customer_code")],
    "Employee": [("sales", "employee_id"), ("hr", "employee_id"), ("hr", "manager_id")],
    "Product": [("sales", "product_id"), ("inventory", "product_id"), ("products", "product_id"), ("product_catalog", "sku")],
    "Order": [("sales", "order_id"), ("finance_invoices", "order_id")],
}
RELATIONSHIPS = [  # (from table, from col, to table, to col, type)
    ("sales", "customer_id", "accounts", "customer_code", "many_to_one"),
    ("sales", "product_id", "products", "product_id", "many_to_one"),
    ("sales", "employee_id", "hr", "employee_id", "many_to_one"),
    ("finance_invoices", "order_id", "sales", "order_id", "one_to_one"),
    ("finance_invoices", "customer_id", "accounts", "customer_code", "many_to_one"),
    ("support", "customer_id", "accounts", "customer_code", "many_to_one"),
    ("inventory", "product_id", "products", "product_id", "many_to_one"),
    ("contacts", "customer_code", "accounts", "customer_code", "many_to_one"),
    ("product_catalog", "sku", "products", "product_id", "one_to_one"),
]


def main(reset: bool = False) -> None:
    if reset:
        for p in [settings.data_dir / "metadata.db", settings.data_dir / "metadata.db-wal", settings.data_dir / "metadata.db-shm"]:
            p.unlink(missing_ok=True)
        for p in settings.duckdb_dir.glob("*.duckdb"):
            p.unlink()
    bootstrap()
    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.role == "admin"))
        # users
        for email, name, role, dept in [("analyst@example.com", "Asha Analyst", "analyst", "Finance"), ("viewer@example.com", "Vikram Viewer", "viewer", "Sales")]:
            if not db.scalar(select(User).where(User.email == email)):
                db.add(User(email=email, name=name, password_hash=hash_password("demo1234"), role=role, department=dept))
        db.commit()
        # sources
        for spec in SOURCES:
            if db.scalar(select(DataSource).where(DataSource.name == spec["name"])):
                continue
            src = DataSource(**spec)
            db.add(src)
            db.commit()
            log.info("syncing %s", src.name)
            log.info("  %s", full_sync(db, src.id))
        db.commit()

        tables = {t.table_name: t for t in db.scalars(select(Table)).all()}
        cols = {(t.table_name, c.column_name): c for t in tables.values() for c in t.columns}
        # nicer table metadata
        for name, (bn, desc, dom) in {
            "sales": ("Sales Orders", "One row per customer order with product, sales rep, region, channel, discount, revenue and delivery outcome.", "sales"),
            "products": ("Product Master", "Product catalogue with category and list price.", "product"),
            "finance_invoices": ("Invoices", "Invoices raised per order with posting status, amount and cost of goods; official revenue source.", "finance"),
            "finance_budget": ("Departmental Budget", "Monthly budget vs actual spend by department.", "finance"),
            "hr": ("Employee Master", "All employees with department, region, role, hire/exit dates and salary.", "hr"),
            "inventory": ("Inventory Snapshots", "Stock units and availability percentage per product, region and snapshot date (every 3 days).", "inventory"),
            "support": ("Support Tickets", "Helpdesk tickets with issue type, priority, resolution time, SLA breach flag and CSAT.", "support"),
            "marketing": ("Marketing Campaigns", "Monthly campaign spend, impressions, leads and conversions by region and channel.", "marketing"),
            "accounts": ("CRM Accounts", "Customer accounts in the CRM (customer_code = customer_id elsewhere).", "customer"),
            "contacts": ("CRM Contacts", "People at customer accounts (PII).", "customer"),
            "product_catalog": ("CRM Product Catalog", "Products as known to the CRM (sku = product_id).", "product"),
            "customers": ("Customers (export)", "Flat export of customer master data.", "customer"),
        }.items():
            if name in tables:
                t = tables[name]
                t.business_name, t.description, t.business_domain = bn, desc, dom
        for (tn, cn), (bname, desc, unit) in {
            ("sales", "revenue"): ("Revenue", "Order revenue after discount = quantity × unit_price × (1 − discount%).", "INR"),
            ("sales", "delivery_delayed"): ("Delivery Delayed", "1 if the order was delivered more than 7 days after order date.", None),
            ("sales", "discount_pct"): ("Discount %", "Discount percentage applied to the order.", "%"),
            ("hr", "exit_date"): ("Exit Date", "Employee separation date; NULL for active employees.", None),
            ("inventory", "availability_pct"): ("Availability %", "Share of the day the product was orderable in the region.", "%"),
            ("finance_invoices", "invoice_status"): ("Invoice Status", "POSTED = recognised revenue; CANCELLED = excluded from revenue.", None),
            ("support", "sla_breached"): ("SLA Breached", "True when resolution exceeded the 48-hour SLA.", None),
        }.items():
            if (tn, cn) in cols:
                c = cols[(tn, cn)]
                c.business_name, c.business_definition, c.unit = bname, desc, unit
        # metrics
        for m in METRICS:
            if db.scalar(select(Metric).where(Metric.name == m["name"])):
                continue
            t = tables.get(m.pop("table"))
            if t is None:
                log.warning("metric %s: table missing", m["name"])
                continue
            metric = Metric(table_id=t.id, **m)
            db.add(metric)
            db.flush()
            record_version(db, "metric", metric, None, admin.email)
        # glossary
        for g in GLOSSARY:
            if not db.scalar(select(GlossaryTerm).where(GlossaryTerm.term == g["term"])):
                gt = GlossaryTerm(**g)
                db.add(gt)
                db.flush()
                record_version(db, "glossary", gt, None, admin.email)
        # entities
        for name, members in ENTITIES.items():
            ent = db.scalar(select(Entity).where(Entity.name == name))
            if ent is None:
                ent = Entity(name=name, description=f"Canonical {name} entity resolved across systems.")
                db.add(ent)
                db.flush()
            mapped = {m.column_id for m in ent.mappings}
            for tn, cn in members:
                c = cols.get((tn, cn))
                if c and c.id not in mapped:
                    db.add(EntityMapping(entity_id=ent.id, column_id=c.id, role="key", confidence=1.0))
        # relationships: approve curated ones, keep others as suggestions
        existing = {(r.from_column_id, r.to_column_id): r for r in db.scalars(select(Relationship)).all()}
        for ft, fc, tt, tc, typ in RELATIONSHIPS:
            a, b = cols.get((ft, fc)), cols.get((tt, tc))
            if not a or not b:
                continue
            r = existing.get((a.id, b.id)) or existing.get((b.id, a.id))
            if r is None:
                r = Relationship(from_column_id=a.id, to_column_id=b.id, type=typ, confidence=1.0, reason="Curated by data steward", is_cross_source=a.table.dataset.source_id != b.table.dataset.source_id)
                db.add(r)
            r.status, r.created_by = "approved", admin.email
        db.commit()
        # permissions: viewers cannot see HR salary or CRM contacts; analysts see HR rows only for Sales/Support depts
        viewer_deny = [c for (tn, cn), c in cols.items() if (tn, cn) in {("hr", "salary"), ("hr", "email"), ("hr", "employee_name")}]
        if not db.scalar(select(Permission).limit(1)):
            for c in viewer_deny:
                db.add(Permission(role="viewer", resource_type="column", resource_id=c.id, access="deny"))
            if "contacts" in tables:
                db.add(Permission(role="viewer", resource_type="table", resource_id=tables["contacts"].id, access="deny"))
            if "hr" in tables:
                db.add(Permission(role="analyst", resource_type="table", resource_id=tables["hr"].id, access="allow", row_filter="department IN ('Sales','Support','Operations','Marketing')"))
                # analysts: default-deny kicks in once an allow rule exists → allow all datasets explicitly
                for ds in {t.dataset for t in tables.values()}:
                    db.add(Permission(role="analyst", resource_type="dataset", resource_id=ds.id, access="allow"))
        db.commit()
        # AI provider: local Ollama if reachable
        if not db.scalar(select(AIProviderConfig).limit(1)):
            try:
                tags = httpx.get("http://localhost:11434/api/tags", timeout=3).json().get("models", [])
                names = [m["name"] for m in tags]
                preferred = next((n for n in ["qwen3:8b", "qwen3:14b", "qwen3-vl:8b", "llama3.1:8b", "qwen2.5:14b-instruct-q4_K_M"] if n in names), names[0] if names else None)
                embed = next((n for n in names if "embed" in n), None)
                if preferred:
                    db.add(AIProviderConfig(name="Local Ollama", provider="ollama", model=preferred, base_url="http://localhost:11434/v1", temperature=0.1, max_tokens=4096, embedding_model=embed, purposes=["reasoning", "planning", "metadata", "summarization"] + (["embeddings"] if embed else []), is_default=True, extra={"num_ctx": 24576}))
                    log.info("configured Ollama provider: %s (embeddings: %s)", preferred, embed)
            except Exception as e:  # noqa: BLE001
                log.info("Ollama not reachable (%s); add an AI provider in Administration → AI Providers", e)
            key = __import__("os").environ.get("OPENAI_API_KEY")
            if key:
                db.add(AIProviderConfig(name="OpenAI", provider="openai", model="gpt-4o-mini", api_key=encrypt(key), embedding_model="text-embedding-3-small", purposes=["embeddings"], input_cost_per_1m=0.15, output_cost_per_1m=0.6))
            db.commit()
        # index + insights
        from backend.ai.service import embed_fn

        fn, model = embed_fn(db)
        log.info("indexed %d catalog objects (embeddings=%s)", reindex_all(db, fn, model), model)
        db.commit()
        from backend.analytics.insights import generate_insights

        created = generate_insights(db, admin)
        db.commit()
        log.info("generated %d insights", len(created))
        for i in created[:5]:
            log.info("  [%s] %s", i.severity, i.title)
    log.info("done. login: %s / %s", settings.default_admin_email, settings.default_admin_password)


if __name__ == "__main__":
    main(reset="--reset" in sys.argv)
