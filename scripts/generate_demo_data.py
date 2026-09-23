"""Generate the synthetic enterprise dataset (Jan 2025 → Sep 2026) with a planted August-2026 scenario:

  West-region sales headcount ↓ (7 reps leave in July)  →  West orders ↓
  Product C availability ↓ in West (supply issue)         →  Product C revenue ↓
  Delivery delays ↑, support complaints ↑ in West
  Marketing spend flat (so it is NOT a contributor)

Outputs: data/samples/{finance.xlsx, sales.csv, hr.csv, inventory.csv, customers.csv, support.json,
marketing.csv, products.csv, crm.sqlite}
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parents[1] / "data" / "samples"
OUT.mkdir(parents=True, exist_ok=True)
rng = np.random.default_rng(42)

REGIONS = ["North", "South", "East", "West"]
CHANNELS = ["Direct", "Partner", "Online"]
PRODUCTS = pd.DataFrame({"product_id": ["P001", "P002", "P003", "P004", "P005"], "product_name": ["Product A", "Product B", "Product C", "Product D", "Product E"], "category": ["Hardware", "Hardware", "Software", "Services", "Software"], "list_price": [12000, 8500, 24000, 5000, 15000]})
MONTHS = pd.period_range("2025-01", "2026-09", freq="M")
SCENARIO_MONTH = pd.Period("2026-08", "M")
FIRST_NAMES = ["Aarav", "Vivaan", "Aditya", "Sai", "Arjun", "Ananya", "Diya", "Isha", "Kavya", "Meera", "Rohan", "Riya", "Neha", "Karan", "Priya", "Rahul", "Sneha", "Vikram", "Pooja", "Nikhil"]
LAST_NAMES = ["Sharma", "Verma", "Patel", "Reddy", "Nair", "Iyer", "Singh", "Gupta", "Mehta", "Joshi", "Kulkarni", "Das", "Bose", "Rao", "Menon"]


def month_days(p: pd.Period) -> list[date]:
    start = p.start_time.date()
    return [start + timedelta(days=i) for i in range(p.days_in_month)]


# ------------------------------------------------------------------ HR
def make_hr() -> pd.DataFrame:
    rows = []
    eid = 1
    for dept, n_per_region in (("Sales", 24), ("Support", 8), ("Operations", 10), ("Marketing", 4), ("Finance", 3), ("Engineering", 12), ("HR", 2)):
        for region in REGIONS:
            n = n_per_region if dept in {"Sales", "Support", "Operations"} else max(1, n_per_region // 2)
            for _ in range(n):
                hire = date(2019, 1, 1) + timedelta(days=int(rng.integers(0, 2200)))
                exit_dt = None
                if rng.random() < 0.12:  # normal attrition
                    exit_dt = hire + timedelta(days=int(rng.integers(200, 2000)))
                    if exit_dt > date(2026, 9, 15) or exit_dt < date(2025, 1, 1):
                        exit_dt = None if rng.random() < 0.5 else date(2025, 1, 1) + timedelta(days=int(rng.integers(0, 600)))
                rows.append({"employee_id": f"E{eid:04d}", "employee_name": f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}", "email": f"emp{eid}@example.com", "department": dept, "region": region, "role": {"Sales": "Sales Executive", "Support": "Support Engineer", "Operations": "Operations Analyst"}.get(dept, f"{dept} Specialist"), "hire_date": hire, "exit_date": exit_dt, "salary": int(rng.integers(400000, 2400000)), "manager_id": None})
                eid += 1
    hr = pd.DataFrame(rows)
    # planted: 7 West sales reps leave in July 2026 (still active in July → counted; gone in August)
    west_sales_active = hr[(hr.department == "Sales") & (hr.region == "West") & hr.exit_date.isna()].index[:7]
    hr.loc[west_sales_active, "exit_date"] = [date(2026, 7, 5 + 3 * i) for i in range(7)]
    # a couple of East sales hires in June 2026 (positive control)
    for i in range(2):
        hr.loc[len(hr)] = {"employee_id": f"E{eid + i:04d}", "employee_name": f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}", "email": f"emp{eid + i}@example.com", "department": "Sales", "region": "East", "role": "Sales Executive", "hire_date": date(2026, 6, 10), "exit_date": None, "salary": 900000, "manager_id": None}
    managers = hr[hr.role.str.contains("Executive")].groupby("region").head(1).set_index("region")["employee_id"]
    hr["manager_id"] = hr.region.map(managers)
    return hr


# ------------------------------------------------------------------ customers
def make_customers(n: int = 400) -> pd.DataFrame:
    segs = ["Enterprise", "Mid-Market", "SMB"]
    inds = ["Manufacturing", "Retail", "BFSI", "Healthcare", "Technology", "Logistics"]
    rows = []
    for i in range(1, n + 1):
        rows.append({"customer_id": f"C{i:04d}", "customer_name": f"{rng.choice(['Apex', 'Nova', 'Vertex', 'Summit', 'Orion', 'Zenith', 'Pioneer', 'Horizon', 'Prime', 'Global'])} {rng.choice(['Industries', 'Systems', 'Retail', 'Logistics', 'Healthcare', 'Technologies', 'Foods', 'Motors'])} {i}", "segment": rng.choice(segs, p=[0.15, 0.35, 0.5]), "region": rng.choice(REGIONS), "industry": rng.choice(inds), "signup_date": date(2018, 1, 1) + timedelta(days=int(rng.integers(0, 2800))), "email": f"contact{i}@customer{i}.com", "phone": f"+91 9{rng.integers(100000000, 999999999)}"})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ inventory
def make_inventory() -> pd.DataFrame:
    rows = []
    for p in MONTHS:
        for pid in PRODUCTS.product_id:
            for region in REGIONS:
                for d in month_days(p)[::3]:  # every 3rd day
                    avail = float(np.clip(rng.normal(92, 4), 70, 100))
                    stock = int(rng.integers(300, 900))
                    if pid == "P003" and region == "West" and p == SCENARIO_MONTH:
                        avail = float(np.clip(rng.normal(56, 6), 35, 70))
                        stock = int(rng.integers(40, 160))
                    if pid == "P003" and region == "West" and p == SCENARIO_MONTH + 1:
                        avail = float(np.clip(rng.normal(75, 5), 60, 85))
                    rows.append({"product_id": pid, "date": d, "region": region, "stock_units": stock, "availability_pct": round(avail, 1)})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ sales + finance
def make_sales(hr: pd.DataFrame, customers: pd.DataFrame, inventory: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    sales_reps = hr[hr.department == "Sales"]
    avail = inventory.assign(month=pd.to_datetime(inventory.date).dt.to_period("M")).groupby(["month", "product_id", "region"]).availability_pct.mean()
    orders, invoices = [], []
    oid = inv = 1
    base_orders_per_rep = 18.0
    PRICES = {r.product_id: r for r in PRODUCTS.itertuples()}
    for p in MONTHS:
        seasonal = 1 + 0.12 * np.sin((p.month - 3) / 12 * 2 * np.pi) + (0.06 if p.year == 2026 else 0)
        for region in REGIONS:
            active = sales_reps[(sales_reps.region == region) & (pd.to_datetime(sales_reps.hire_date) <= p.end_time) & (sales_reps.exit_date.isna() | (pd.to_datetime(sales_reps.exit_date) >= p.start_time))]
            if len(active) == 0:
                continue
            region_factor = {"North": 1.0, "South": 1.1, "East": 0.9, "West": 1.2}[region]
            n_orders = int(rng.poisson(base_orders_per_rep * len(active) * seasonal * region_factor))
            reg_customers = customers[customers.region == region].customer_id.values
            active_ids = active.employee_id.values
            for _ in range(n_orders):
                pid = rng.choice(PRODUCTS.product_id.values, p=[0.25, 0.2, 0.3, 0.1, 0.15])
                a = avail.get((p, pid, region), 92.0)
                if rng.random() > (a / 100) ** 1.2:  # stock-outs kill orders
                    continue
                prod = PRICES[pid]
                qty = int(rng.integers(1, 12))
                disc = float(rng.choice([0, 0, 5, 10, 15], p=[0.4, 0.2, 0.2, 0.15, 0.05]))
                revenue = round(qty * prod.list_price * (1 - disc / 100), 2)
                delivery_days = float(np.clip(rng.normal(5, 1.5), 1, 15))
                if region == "West" and p == SCENARIO_MONTH:
                    delivery_days = float(np.clip(rng.normal(7.5, 2.2), 2, 20))
                od = p.start_time.date() + timedelta(days=int(rng.integers(0, p.days_in_month)))
                rep = active_ids[int(rng.integers(0, len(active_ids)))]
                orders.append({"order_id": f"O{oid:06d}", "order_date": od, "customer_id": rng.choice(reg_customers), "product_id": pid, "employee_id": rep, "region": region, "channel": rng.choice(CHANNELS, p=[0.5, 0.3, 0.2]), "quantity": qty, "unit_price": prod.list_price, "discount_pct": disc, "revenue": revenue, "delivery_days": round(delivery_days, 1), "delivery_delayed": int(delivery_days > 7)})
                status = "POSTED" if rng.random() > 0.04 else "CANCELLED"
                invoices.append({"invoice_id": f"INV{inv:06d}", "order_id": f"O{oid:06d}", "customer_id": orders[-1]["customer_id"], "invoice_date": od + timedelta(days=int(rng.integers(0, 5))), "invoice_amount": revenue, "cost_amount": round(revenue * float(rng.uniform(0.55, 0.72)), 2), "invoice_status": status, "region": region, "currency": "INR"})
                oid += 1
                inv += 1
    return pd.DataFrame(orders), pd.DataFrame(invoices)


# ------------------------------------------------------------------ support
def make_support(customers: pd.DataFrame, orders: pd.DataFrame) -> list[dict]:
    issues = ["Delivery delay", "Product defect", "Billing", "Login issue", "Feature request", "Damaged goods"]
    rows = []
    tid = 1
    monthly_orders = orders.assign(month=pd.to_datetime(orders.order_date).dt.to_period("M")).groupby(["month", "region"]).size()
    for p in MONTHS:
        for region in REGIONS:
            n = int(rng.poisson(0.18 * monthly_orders.get((p, region), 50) + 8))
            issue_p = np.array([0.2, 0.2, 0.2, 0.15, 0.15, 0.1])
            if region == "West" and p == SCENARIO_MONTH:
                n = int(n * 1.9)
                issue_p = np.array([0.5, 0.15, 0.1, 0.08, 0.07, 0.1])
            reg_customers = customers[customers.region == region].customer_id.values
            for _ in range(n):
                issue = str(rng.choice(issues, p=issue_p))
                res = float(np.clip(rng.normal(30 if issue != "Delivery delay" else 48, 12), 2, 200))
                rows.append({"ticket_id": f"T{tid:06d}", "customer_id": str(rng.choice(reg_customers)), "ticket_date": (p.start_time.date() + timedelta(days=int(rng.integers(0, p.days_in_month)))).isoformat(), "issue_type": issue, "priority": str(rng.choice(["Low", "Medium", "High"], p=[0.4, 0.4, 0.2])), "region": region, "resolution_hours": round(res, 1), "sla_breached": bool(res > 48), "csat": int(np.clip(round(rng.normal(4.1 if issue != "Delivery delay" else 3.2, 0.8)), 1, 5))})
                tid += 1
    return rows


# ------------------------------------------------------------------ marketing
def make_marketing() -> pd.DataFrame:
    rows = []
    cid = 1
    for p in MONTHS:
        for region in REGIONS:
            for ch in ["Search", "Social", "Email", "Events"]:
                spend = float(rng.normal({"Search": 180000, "Social": 120000, "Email": 40000, "Events": 250000}[ch], 15000))
                leads = int(spend / rng.uniform(1800, 2600))
                rows.append({"campaign_id": f"MK{cid:05d}", "month": p.start_time.date(), "region": region, "channel": ch, "spend": round(spend, 2), "impressions": int(spend * rng.uniform(4, 8)), "leads": leads, "conversions": int(leads * rng.uniform(0.06, 0.12))})
                cid += 1
    return pd.DataFrame(rows)


def main() -> None:
    hr = make_hr()
    customers = make_customers()
    inventory = make_inventory()
    orders, invoices = make_sales(hr, customers, inventory)
    support = make_support(customers, orders)
    marketing = make_marketing()

    orders.to_csv(OUT / "sales.csv", index=False)
    hr.to_csv(OUT / "hr.csv", index=False)
    inventory.to_csv(OUT / "inventory.csv", index=False)
    customers.to_csv(OUT / "customers.csv", index=False)
    marketing.to_csv(OUT / "marketing.csv", index=False)
    PRODUCTS.to_csv(OUT / "products.csv", index=False)
    with open(OUT / "support.json", "w", encoding="utf-8") as f:
        json.dump({"data": support}, f)
    with pd.ExcelWriter(OUT / "finance.xlsx") as xw:
        invoices.to_excel(xw, sheet_name="invoices", index=False)
        budget = pd.DataFrame([{"month": p.start_time.date(), "department": d, "budget": int(rng.integers(2_000_000, 9_000_000)), "actual": int(rng.integers(1_800_000, 9_500_000))} for p in MONTHS for d in ["Sales", "Marketing", "Operations", "Engineering", "Support"]])
        budget.to_excel(xw, sheet_name="budget", index=False)
    # CRM in SQLite: customers + products (different key naming to exercise entity resolution)
    db = OUT / "crm.sqlite"
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    crm = customers.rename(columns={"customer_id": "customer_code", "customer_name": "account_name"})
    crm["signup_date"] = crm["signup_date"].astype(str)
    crm.to_sql("accounts", con, index=False)
    PRODUCTS.rename(columns={"product_id": "sku"}).to_sql("product_catalog", con, index=False)
    con.execute("CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, customer_code TEXT, contact_name TEXT, email TEXT, phone TEXT)")
    con.executemany("INSERT INTO contacts VALUES (?,?,?,?,?)", [(f"CT{i:04d}", c, f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}", f"person{i}@example.com", f"+91 8{rng.integers(100000000, 999999999)}") for i, c in enumerate(customers.customer_id, 1)])
    con.commit()
    con.close()

    m = orders.assign(month=pd.to_datetime(orders.order_date).dt.to_period("M")).groupby("month").revenue.sum()
    print("monthly revenue (last 4):", {str(k): round(v / 1e7, 2) for k, v in m.tail(4).items()}, "Cr")
    west = orders[orders.region == "West"].assign(month=pd.to_datetime(orders.order_date).dt.to_period("M")).groupby("month").revenue.sum()
    print("west revenue Jul->Aug:", round(west[pd.Period("2026-07", "M")] / 1e7, 2), "->", round(west[SCENARIO_MONTH] / 1e7, 2), "Cr")
    print("rows:", {"sales": len(orders), "invoices": len(invoices), "hr": len(hr), "inventory": len(inventory), "support": len(support), "marketing": len(marketing), "customers": len(customers)})


if __name__ == "__main__":
    main()
