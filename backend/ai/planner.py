"""Question → structured query plan (intent, metric, periods, dimensions, domains).
Heuristics first (fast, deterministic, works offline); an LLM refinement pass fills gaps when the
catalog match is ambiguous. Conversation context resolves follow-ups ("was it because of attrition?")."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.ai.providers import AIProvider
from backend.metadata.models import GlossaryTerm, Metric, Table
from backend.query_engine.periods import Period, comparison_for, default_period, resolve_period
from backend.vector_store.store import CatalogVectorStore, tokenize

INTENT_RULES = [
    ("diagnostic", r"\b(why|cause|reason|driv(e|ing|en)|because|explain|root cause|what happened to|contribut|associated|affect(s|ed|ing)?|impact(s|ed|ing)?|linked|related to|due to)\b"),
    ("forecast", r"\b(forecast|predict|projection|next (month|quarter|year)|will .* be|expect)\b"),
    ("anomaly", r"\b(anomal|unusual|outlier|spike|abnormal|unexpected|risk)\b"),
    ("comparative", r"\b(compare|versus|vs\.?|compared|difference between|than last|year over year|yoy|month over month|mom)\b"),
    ("trend", r"\b(trend|over time|monthly|history|evolution|trajectory|growth)\b"),
    ("segmentation", r"\b(which|who|top|bottom|best|worst|most|least|breakdown|by (region|product|customer|department|segment|channel))\b"),
    ("descriptive", r"\b(what is|what was|how much|how many|total|current|show me|list|give me)\b"),
]
FOLLOW_UP = re.compile(r"\b(it|that|this|those|these|the decline|the drop|the change|the increase|same|also|and what about|what about)\b", re.I)
DIRECTION_WORDS = {"down": r"\b(decline|declined|drop|dropped|fall|fell|decrease|decreased|down|lower|shrink|slump|dip)\b", "up": r"\b(increase|increased|rise|rose|grow|grew|growth|spike|jump|up|higher|surge)\b", "any": r"\b(change|changed|moved|shift|shifted|anomal|unusual)\b"}
CAUSAL_FOLLOW_UP = re.compile(r"\b(because of|due to|caused by|cause of|driven by|related to|linked to|responsible|explain(?:s|ed)? (?:it|this|that)|role of|impact of|effect of|associated with)\b", re.I)
GENERIC = {"rate", "count", "average", "avg", "total", "number", "pct", "percent", "the", "and", "what", "which", "how", "many", "much", "was", "were", "did", "in", "of", "for", "by", "is", "are", "most", "last", "this", "that", "it"}
DOMAIN_WORDS = {
    "finance": ["revenue", "invoice", "profit", "margin", "cost", "expense", "finance", "ebitda"],
    "sales": ["sales", "order", "deal", "pipeline", "conversion", "discount", "salesperson", "rep"],
    "hr": ["employee", "attrition", "headcount", "hiring", "recruit", "attendance", "absente", "workforce", "staff", "hr"],
    "customer": ["customer", "client", "churn", "account"],
    "support": ["support", "ticket", "complaint", "sla", "satisfaction", "csat"],
    "inventory": ["inventory", "stock", "availability", "shortage", "warehouse"],
    "operations": ["delivery", "delay", "production", "operations", "logistics", "shipment"],
    "marketing": ["marketing", "campaign", "lead", "spend", "ad "],
    "procurement": ["vendor", "supplier", "procurement", "purchase"],
}


@dataclass
class QueryPlan:
    question: str
    intent: str = "descriptive"
    metric: str | None = None
    metric_id: str | None = None
    period: dict | None = None
    comparison: dict | None = None
    dimensions: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    candidate_tables: list[str] = field(default_factory=list)
    is_follow_up: bool = False
    focus: str | None = None  # a secondary metric/domain the user asks about as a possible cause
    period_source: str = "default"  # explicit | context | default | detected
    direction: str | None = None  # down | up | any — when the question refers to a decline/increase
    inherited: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    refined_by_llm: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def period_obj(self) -> Period | None:
        return _p(self.period)

    def comparison_obj(self) -> Period | None:
        return _p(self.comparison)


def _p(d: dict | None) -> Period | None:
    return Period(date.fromisoformat(d["start"]), date.fromisoformat(d["end"]), d["label"], d.get("grain", "month")) if d else None


def detect_intent(q: str) -> str:
    ql = q.lower()
    for intent, pattern in INTENT_RULES:
        if re.search(pattern, ql):
            return intent
    return "general"


def match_metric(db: Session, q: str) -> Metric | None:
    m, _ = match_metric_ex(db, q)
    return m


def match_metric_ex(db: Session, q: str) -> tuple[Metric | None, bool]:
    """Returns (metric, exact) where exact=True means the metric name/synonym literally appears in the question."""
    ql = q.lower()
    metrics = db.scalars(select(Metric).options(selectinload(Metric.table))).all()
    best, best_len = None, 0
    for m in metrics:
        names = [m.name.lower(), (m.display_name or "").lower()]
        g = db.scalar(select(GlossaryTerm).where(GlossaryTerm.term.ilike(m.display_name or m.name)))
        if g:
            names += [s.lower() for s in (g.synonyms or [])]
        for n in names:
            if n and re.search(rf"\b{re.escape(n)}\b", ql) and len(n) > best_len:
                best, best_len = m, len(n)
    if best:
        return best, True
    # fall back: a metric whose *name* shares a meaningful token with the question (e.g. "attrition" ~ "employee_exits"? no; "tickets" ~ "support_tickets" yes)
    qt = {t for t in tokenize(q) if t not in GENERIC}
    best, best_overlap = None, 0
    for m in metrics:
        names = {t for t in tokenize(m.name + " " + (m.display_name or "")) if t not in GENERIC}
        overlap = sum(1 for a in qt for b in names if a == b or (len(a) > 4 and (a.startswith(b) or b.startswith(a))))
        if overlap > best_overlap:
            best, best_overlap = m, overlap
    return best, False


def detect_dimensions(q: str, metric: Metric | None) -> list[str]:
    if not metric or not metric.table:
        return []
    ql = tokenize(q)
    out = []
    for c in metric.table.columns:
        stem = c.column_name.lower().replace("_id", "").replace("_", " ")
        if c.is_primary_key or (c.stats or {}).get("is_unique"):
            continue  # row identifiers are not decomposition dimensions
        if any(qt == st or (len(st) > 3 and qt.startswith(st)) for st in tokenize(stem) for qt in ql) and c.semantic_type in {"dimension", "identifier"}:
            out.append(c.column_name)
    return out


def detect_domains(q: str) -> list[str]:
    ql = q.lower()
    return [d for d, words in DOMAIN_WORDS.items() if any(w in ql for w in words)]


def plan_question(db: Session, question: str, anchor: date, context: dict | None = None, provider: AIProvider | None = None) -> QueryPlan:
    plan = QueryPlan(question=question, intent=detect_intent(question))
    metric, exact = match_metric_ex(db, question)
    ctx = context or {}
    short = len(question.split()) <= 6
    if metric is not None and not exact and ctx.get("metric_id") and short:
        metric = None  # "Which employees?" inside a revenue analysis is about revenue, not an employee metric
    follow_up = bool(ctx.get("metric")) and (metric is None or FOLLOW_UP.search(question) is not None) and len(question.split()) <= 20
    causal = follow_up and CAUSAL_FOLLOW_UP.search(question) is not None and ctx.get("metric_id")
    if causal and (metric is None or metric.id != ctx["metric_id"]):
        # "was it because of attrition?" → keep the prior metric as the subject, treat the new one as the focus
        plan.focus = (metric.display_name or metric.name) if metric else " ".join(detect_domains(question)) or None
        metric = db.get(Metric, ctx["metric_id"])
        plan.inherited.append("metric")
        plan.intent = "diagnostic"
    elif metric is None and follow_up and ctx.get("metric_id"):
        metric = db.get(Metric, ctx["metric_id"])
        plan.inherited.append("metric")
    plan.is_follow_up = follow_up
    period = resolve_period(question, anchor)
    if period is not None:
        plan.period_source = "explicit"
    ql_ = question.lower()
    for d, pat in DIRECTION_WORDS.items():
        if re.search(pat, ql_):
            plan.direction = d
            break
    comparison_only = follow_up and ctx.get("period") and re.search(r"\b(compared|compare|vs\.?|versus|than|same period|year over year|yoy)\b", question.lower())
    if (period is None or comparison_only) and follow_up and ctx.get("period"):
        period = _p(ctx["period"])
        plan.period_source = "context"
        plan.inherited.append("period")
    if metric:
        plan.metric, plan.metric_id = metric.name, metric.id
    if plan.intent in {"diagnostic", "comparative", "anomaly", "trend", "segmentation", "descriptive"} or metric:
        period = period or default_period(anchor)
        plan.period = period.to_dict()
        comp = comparison_for(question, period)
        if follow_up and ctx.get("comparison") and "period" in plan.inherited and not comparison_only:
            comp = _p(ctx["comparison"]) or comp
        plan.comparison = comp.to_dict()
    plan.dimensions = detect_dimensions(question, metric)
    plan.domains = detect_domains(question)
    if follow_up and plan.intent in {"general", "descriptive"} and ctx.get("intent") == "diagnostic" and FOLLOW_UP.search(question):
        plan.intent = "diagnostic"
        plan.inherited.append("intent")
    # candidate tables from catalog search
    hits = CatalogVectorStore(db).search(question, None, top_k=30, object_types=["table", "column"])
    seen: list[str] = []
    for h in hits:
        tname = None
        if h.object_type == "table":
            t = db.get(Table, h.object_id)
            tname = t.qualified_name if t else None
        else:
            from backend.metadata.models import Column

            c = db.get(Column, h.object_id)
            tname = c.table.qualified_name if c else None
        if tname and tname not in seen:
            seen.append(tname)
        if len(seen) >= 8:
            break
    plan.candidate_tables = seen
    if provider is not None and (metric is None and plan.intent in {"diagnostic", "comparative", "descriptive", "trend", "segmentation"}):
        _refine_with_llm(db, plan, provider, anchor)
    plan.steps = _steps(plan)
    return plan


def _refine_with_llm(db: Session, plan: QueryPlan, provider: AIProvider, anchor: date) -> None:
    metrics = db.scalars(select(Metric)).all()
    schema = {"type": "object", "properties": {"intent": {"type": "string", "enum": ["descriptive", "diagnostic", "comparative", "trend", "segmentation", "anomaly", "forecast", "general"]}, "metric": {"type": ["string", "null"], "enum": [m.name for m in metrics] + [None]}, "period": {"type": ["string", "null"], "description": "e.g. 'August 2026' or '2026-Q2' or null"}, "domains": {"type": "array", "items": {"type": "string"}}}, "required": ["intent", "metric"]}
    prompt = f"Latest data date: {anchor.isoformat()}.\nGoverned metrics: " + "; ".join(f"{m.name}: {m.description or ''}" for m in metrics) + f"\n\nQuestion: {plan.question}\nIdentify the intent, the single most relevant governed metric (or null), the time period mentioned, and business domains involved."
    try:
        out = provider.structured_output(schema, prompt, system="You classify analytics questions for an enterprise data assistant.")
    except Exception:  # noqa: BLE001
        return
    plan.refined_by_llm = True
    if out.get("metric"):
        m = next((x for x in metrics if x.name == out["metric"]), None)
        if m:
            plan.metric, plan.metric_id = m.name, m.id
            plan.dimensions = plan.dimensions or detect_dimensions(plan.question, m)
    if out.get("intent") in {i for i, _ in INTENT_RULES} | {"general"} and plan.intent == "general":
        plan.intent = out["intent"]
    if out.get("period") and not plan.period:
        p = resolve_period(str(out["period"]), anchor)
        if p:
            plan.period, plan.comparison = p.to_dict(), comparison_for(plan.question, p).to_dict()
    if out.get("domains"):
        plan.domains = sorted(set(plan.domains) | {d for d in out["domains"] if isinstance(d, str)})


def _steps(plan: QueryPlan) -> list[str]:
    if plan.intent == "diagnostic" and plan.metric:
        return [f"Validate {plan.metric} change ({plan.comparison['label'] if plan.comparison else 'prior'} → {plan.period['label'] if plan.period else 'latest'})", "Decompose by available dimensions (region, product, customer, channel…)", "Traverse relationship graph to related domains", "Measure related metrics over the same periods", "Check data quality and freshness of every source used", "Correlate monthly series", "Synthesise evidence-backed explanation"]
    if plan.intent in {"comparative", "trend", "anomaly"} and plan.metric:
        return [f"Compute {plan.metric} for the requested periods", "Compare / detect anomalies", "Decompose by key dimensions", "Explain with evidence"]
    if plan.metric:
        return [f"Locate governed definition of {plan.metric}", "Compute for requested period", "Answer with evidence"]
    return ["Search semantic catalog for relevant tables", "Inspect schema and definitions", "Run targeted queries", "Answer with evidence"]
