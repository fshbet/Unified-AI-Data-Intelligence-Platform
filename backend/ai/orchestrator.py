"""AI orchestrator: question → plan → deterministic investigation → LLM tool loop → streamed,
evidence-cited answer. Yields events so the API can stream progress to the UI.

Never sends raw tables to the LLM. The model sees: the plan, compact investigation results (all with
query refs), the metric catalogue and whatever it fetches through permission-checked tools."""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.ai.planner import QueryPlan, plan_question
from backend.ai.providers import AIProvider, AIProviderError
from backend.ai.service import UsageTracker, get_provider
from backend.ai.tools import ToolBox, tool_definitions
from backend.analytics.insights import data_anchor
from backend.analytics.investigation import Investigation, investigate
from backend.core.config import settings
from backend.metadata.models import Conversation, DataSource, Message, Metric, User
from backend.query_engine.periods import Period, shift
from backend.security.access import build_access_context

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an enterprise data analyst working inside a governed data intelligence platform.
You reason over the organisation's whole data ecosystem (finance, sales, HR, inventory, support, marketing, operations...), not a single table.

HARD RULES
- Never fabricate numbers. Every figure you state must come from the investigation results or a tool result, and must cite its query reference like [Q-000123].
- No evidence → no factual claim. If data is unavailable say so. If inconclusive say so.
- Distinguish clearly: observed fact, calculated metric, correlation, potential explanation, strong evidence, weak evidence, unknown. Never present correlation as causation.
- Prefer governed metric definitions over ad-hoc calculations. Respect data-quality warnings and stale data; mention them when they affect confidence.
- If sources disagree, show both values and the difference; do not silently pick one.
- Only READ-ONLY SQL. Aggregate in the database. Use the dialect returned by get_schema.
- Respect permissions: if a tool says access is denied, say the data is restricted for this user.

ANSWER FORMAT (Markdown). For analytical questions use these sections:
## Executive Summary
## Key Findings  (numbered, each with [Q-ref])
## Cross-Domain Evidence  (grouped by domain; label each as Observed fact / Correlation / Potential explanation)
## Likely Contributors  (Evidence-supported / Potential / Insufficient evidence)
## Recommended Investigation
## Confidence  (High / Medium / Low, with reasons and caveats: stale data, quality issues, gaps)
For simple factual questions answer concisely with the number, period, and [Q-ref].
Use the currency/unit of the metric. Be specific and quantitative. Do not restate the raw JSON."""


def _fmt_ctx(metric: Metric | None, plan: QueryPlan, investigation: Investigation | None, anchor: date, sources: list[DataSource], history_ctx: dict) -> str:
    parts = [f"Latest data date (treat as 'today'): {anchor.isoformat()}", "Query plan: " + json.dumps(plan.to_dict(), default=str)]
    parts.append("Data sources: " + "; ".join(f"{s.name} ({s.type}, last sync {s.last_sync_at.isoformat() if s.last_sync_at else 'never'})" for s in sources))
    if history_ctx:
        parts.append("Previous analytical context: " + json.dumps(history_ctx, default=str)[:1500])
    if metric is not None and not metric.is_computable:
        parts.append(
            f"IMPORTANT: '{metric.display_name or metric.name}' was imported from {metric.source_system} as a "
            f"definition-only measure. Its native {(metric.native_language or '').upper()} formula is: "
            f"{metric.native_expression}. This platform cannot compute it (no exact SQL equivalent), so you must "
            f"explain what it means and state plainly that no value can be computed here — never estimate one. "
            f"Suggest the closest computable governed metric if one exists.")
    if plan.focus:
        parts.append(f"The user asks whether '{plan.focus}' explains the {metric.display_name or metric.name if metric else 'metric'} change. Centre the answer on that relationship: what the evidence shows, what it does not, and whether timing supports it (a change in the prior month can have a lagged effect).")
    if investigation:
        parts.append("Deterministic investigation results (all numbers already computed with query refs):\n" + json.dumps(investigation.compact(), default=str))
    return "\n\n".join(parts)


def run(db: Session, user: User, conversation: Conversation, question: str) -> Iterator[dict[str, Any]]:
    """Generator of events: plan, investigation, tool, token, done, error."""
    ctx = build_access_context(db, user)
    anchor = data_anchor(db)
    got = get_provider(db, "reasoning")
    provider, cfg = got if got else (None, None)
    planner_prov = (get_provider(db, "planning") or got or (None, None))[0]
    usage = UsageTracker(db, cfg, user.id, conversation.id, question)
    yield {"type": "status", "message": "Planning investigation"}
    plan = plan_question(db, question, anchor, conversation.context, planner_prov)
    metric = db.get(Metric, plan.metric_id) if plan.metric_id else None
    if metric and plan.direction and plan.period_source == "default":
        _detect_recent_change(db, ctx, metric, plan, anchor)
    yield {"type": "plan", "plan": plan.to_dict()}

    investigation: Investigation | None = None
    if metric and not metric.is_computable:
        yield {"type": "status", "message": f"{metric.display_name or metric.name} is a definition-only metric — explaining its definition"}
    elif metric and plan.intent in {"diagnostic", "comparative", "anomaly", "trend", "segmentation", "descriptive"} and plan.period:
        yield {"type": "status", "message": f"Investigating {metric.display_name or metric.name} across connected domains"}
        try:
            investigation = investigate(db, ctx, metric, plan.period_obj(), plan.comparison_obj() or shift(plan.period_obj(), -1), conversation.id, max_hops=3 if plan.intent == "diagnostic" else 1)
            db.commit()
            yield {"type": "investigation", "investigation": investigation.to_dict()}
        except Exception as e:  # noqa: BLE001
            log.exception("investigation failed")
            db.rollback()
            yield {"type": "status", "message": f"Investigation engine error: {e}"}

    sources = db.scalars(select(DataSource).where(DataSource.is_enabled)).all()
    toolbox = ToolBox(db, ctx, conversation.id, anchor)
    history = [m for m in conversation.messages if m.role in {"user", "assistant"}][-6:]
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in history:
        messages.append({"role": m.role, "content": m.content[:4000]})
    messages.append({"role": "system", "content": _fmt_ctx(metric, plan, investigation, anchor, sources, conversation.context or {})})
    messages.append({"role": "user", "content": question})

    answer = ""
    if provider is None:
        yield {"type": "status", "message": "No AI provider configured — rendering deterministic analysis"}
        answer = template_answer(plan, investigation, toolbox, db, ctx)
        yield {"type": "token", "text": answer}
    else:
        try:
            rich = investigation is not None and len(investigation.evidence) >= 5
            for ev in _tool_loop(provider, messages, toolbox, usage, plan, max_iter=2 if rich else settings.ai_max_tool_iterations):
                yield ev
            messages.append({"role": "system", "content": "Write the final answer now. Cover EVERY evidence item and every domain listed in the investigation (finance, sales, hr, inventory, operations, support, marketing...) — do not omit HR headcount/attrition or inventory availability findings when present. Cite query refs. If the deterministic investigation and tool results disagree, say so."})
            yield {"type": "status", "message": "Writing answer"}
            for delta in provider.stream(messages):
                answer += delta
                yield {"type": "token", "text": delta}
            if not answer.strip():
                answer = template_answer(plan, investigation, toolbox, db, ctx)
                yield {"type": "token", "text": answer}
            usage.add(_approx_usage(messages, answer), "reasoning")
        except AIProviderError as e:
            log.warning("AI provider error: %s", e)
            yield {"type": "status", "message": f"AI provider error ({e}); falling back to deterministic answer"}
            answer = template_answer(plan, investigation, toolbox, db, ctx)
            yield {"type": "token", "text": answer}

    answer, unverified = validate_citations(answer, (investigation.queries if investigation else []) + toolbox.queries)
    payload = build_payload(plan, investigation, toolbox, usage, answer)
    if unverified:
        payload["warnings"].append(f"{len(unverified)} citation(s) in the narrative do not match any executed query and were marked unverified: {', '.join(unverified[:5])}")
        if payload["confidence"]["level"] == "high":
            payload["confidence"]["level"] = "medium"
        payload["confidence"]["reasons"].append(f"{len(unverified)} unverified citation(s)")
    msg = Message(conversation_id=conversation.id, role="assistant", content=answer, payload=payload)
    db.add(msg)
    conversation.context = {"metric": plan.metric, "metric_id": plan.metric_id, "period": plan.period, "comparison": plan.comparison, "intent": plan.intent, "top_findings": [e["statement"] for e in payload["evidence"][:4]]}
    if conversation.title == "New analysis":
        conversation.title = question[:80]
    db.commit()
    yield {"type": "done", "message_id": msg.id, "payload": payload, "content": answer}


def _detect_recent_change(db: Session, ctx, metric: Metric, plan: QueryPlan, anchor: date, months: int = 8, threshold: float = 5.0) -> None:
    """'Why did revenue drop?' with no period → anchor on the most recent month that moved ≥ threshold% in that direction."""
    from backend.query_engine import metrics as M
    from backend.query_engine.periods import default_period

    try:
        cur = default_period(anchor)
        series, _ = M.monthly_series(db, ctx, metric, shift(cur, -(months - 1)).start, cur.end, None)
    except Exception:  # noqa: BLE001
        return
    keys = sorted(series)
    for i in range(len(keys) - 1, 0, -1):
        ch = M.pct_change(series[keys[i - 1]], series[keys[i]])
        if ch is None:
            continue
        hit = (plan.direction == "down" and ch <= -threshold) or (plan.direction == "up" and ch >= threshold) or (plan.direction == "any" and abs(ch) >= threshold)
        if hit:
            y, m = map(int, keys[i].split("-"))
            p = Period(date(y, m, 1), shift(Period(date(y, m, 1), date(y, m, 1), "", "month"), 1).start, date(y, m, 1).strftime("%B %Y"), "month")
            plan.period, plan.comparison, plan.period_source = p.to_dict(), shift(p, -1).to_dict(), "detected"
            plan.steps.insert(0, f"No period stated: anchored on {p.label}, the most recent month with a {abs(ch):.1f}% {'drop' if ch < 0 else 'rise'}")
            return


def _tool_loop(provider: AIProvider, messages: list[dict], toolbox: ToolBox, usage: UsageTracker, plan: QueryPlan, max_iter: int) -> Iterator[dict]:
    """Let the model call tools to gather more evidence; stop when it returns a plain answer, repeats itself or hits the cap."""
    tools = tool_definitions()
    prelude = {"role": "system", "content": ("The deterministic investigation above already gathered evidence across every connected domain. " if max_iter <= 2 else "") + "Before answering, decide whether a specific fact is still missing. If so call a tool (each returns query refs). Never repeat a call you already made. When you have enough, reply with the single word READY."}
    work = messages + [prelude]
    seen: set[str] = set()
    for i in range(max_iter):
        try:
            resp = provider.chat(work, tools=tools)
        except AIProviderError as e:
            if "tool" in str(e).lower() and i == 0:  # model has no tool support: skip the loop
                yield {"type": "status", "message": "Model does not support tool calling; using investigation results only"}
                return
            raise
        usage.add(resp, "reasoning")
        if not resp.tool_calls:
            break
        work.append(resp.raw_assistant_message or {"role": "assistant", "content": resp.content})
        repeats = 0
        for tc in resp.tool_calls:
            key = tc.name + json.dumps(tc.arguments, sort_keys=True, default=str)
            if key in seen:
                repeats += 1
                work.append({"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": json.dumps({"note": "duplicate call — result already provided above"})})
                continue
            seen.add(key)
            yield {"type": "tool", "name": tc.name, "args": tc.arguments}
            result = toolbox.call(tc.name, tc.arguments)
            work.append({"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": result})
            yield {"type": "tool_result", "name": tc.name, "preview": result[:300]}
        if repeats and repeats == len(resp.tool_calls):
            break  # model is looping
    # fold gathered evidence into the final-answer messages (tool transcripts are dropped, results kept)
    if toolbox.queries or toolbox.calls:
        gathered = {"tool_results": [{"query_ref": q.get("ref"), "purpose": q.get("purpose"), "columns": q.get("columns"), "rows": q.get("rows", [])[:15], "error": q.get("error")} for q in toolbox.queries[-12:]]}
        messages.append({"role": "system", "content": "Additional evidence gathered via tools: " + json.dumps(gathered, default=str)[:8000]})


def validate_citations(answer: str, queries: list[dict]) -> tuple[str, list[str]]:
    """Every Q-ref the model cites must be a query that actually ran. Unknown refs are marked, never silently kept."""
    import re

    known = {q.get("ref") for q in queries}
    unknown: list[str] = []

    def mark(m: re.Match) -> str:
        ref = m.group(0)
        if ref in known:
            return ref
        if ref not in unknown:
            unknown.append(ref)
        return f"{ref}⚠unverified"

    return re.sub(r"Q-[0-9A-Fa-f]{6,8}", mark, answer), unknown


def _approx_usage(messages: list[dict], answer: str):
    from backend.ai.providers import ChatResponse

    return ChatResponse(answer, [], sum(len(str(m.get("content", ""))) for m in messages) // 4, len(answer) // 4, 0)


def build_payload(plan: QueryPlan, inv: Investigation | None, toolbox: ToolBox, usage: UsageTracker, answer: str) -> dict[str, Any]:
    queries = (inv.queries if inv else []) + toolbox.queries
    evidence = [e.to_dict() for e in inv.evidence] if inv else []
    sources = sorted(set(inv.sources if inv else []) | toolbox.sources_used)
    tables = sorted(set(inv.tables if inv else []) | toolbox.tables_used)
    charts = auto_charts(inv) + toolbox.charts
    return {
        "plan": plan.to_dict(), "investigation": inv.to_dict() if inv else None, "queries": queries, "evidence": evidence, "sources": sources, "tables": tables,
        "charts": charts, "reasoning_path": inv.reasoning_path if inv else [], "confidence": confidence(inv, answer), "usage": usage.summary(), "tool_calls": toolbox.calls,
        "warnings": inv.warnings if inv else [], "follow_ups": follow_ups(plan, inv),
    }


def confidence(inv: Investigation | None, answer: str) -> dict[str, Any]:
    if not inv:
        return {"level": "medium" if "[Q-" in answer else "low", "reasons": ["No deterministic investigation was run"]}
    strong = sum(1 for e in inv.evidence if e.strength == "strong")
    reasons = [f"{strong} strong and {len(inv.evidence) - strong} supporting evidence items"]
    level = "high" if strong >= 2 else ("medium" if inv.evidence else "low")
    if inv.headline.get("change_pct") is None:
        level = "low"
        reasons.insert(0, "The primary metric could not be computed for both periods")
    if inv.quality_issues:
        reasons.append(f"{len(inv.quality_issues)} open data-quality issue(s) on involved tables")
        level = "medium" if level == "high" else level
    stale = [f["source"] for f in inv.freshness if f["age_minutes"] is not None and f["age_minutes"] > 60 * 24 * 3]
    if stale:
        reasons.append(f"Stale sources: {', '.join(stale)}")
        level = "medium" if level == "high" else level
    if inv.warnings:
        reasons.append(f"{len(inv.warnings)} warning(s) during analysis")
    return {"level": level, "reasons": reasons}


def auto_charts(inv: Investigation | None) -> list[dict]:
    if not inv:
        return []
    charts = []
    if inv.trend:
        charts.append({"type": "line", "title": f"{inv.headline['metric']} — monthly trend", "x": [t["month"] for t in inv.trend], "series": [{"name": inv.headline["metric"], "data": [t["value"] for t in inv.trend]}], "format": inv.headline.get("format"), "unit": inv.headline.get("unit")})
    for d in inv.decompositions[:2]:
        segs = d["segments"][:8]
        charts.append({"type": "bar", "title": f"{inv.headline['metric']} by {d['dimension']}: {inv.comparison.label} vs {inv.period.label}", "x": [s["segment"] for s in segs], "series": [{"name": inv.comparison.label, "data": [s["before"] for s in segs]}, {"name": inv.period.label, "data": [s["after"] for s in segs]}], "query_ref": d["query_refs"][-1]})
    rel = [r for r in inv.related if r["change_pct"] is not None][:8]
    if rel:
        charts.append({"type": "bar", "title": "Related metrics — % change over the same periods", "x": [r["metric"] for r in rel], "series": [{"name": "% change", "data": [r["change_pct"] for r in rel]}], "format": "percent"})
    return charts


def follow_ups(plan: QueryPlan, inv: Investigation | None) -> list[str]:
    if not inv:
        return ["Which data sources are connected?", "What business metrics are defined?", "Show me open data-quality issues"]
    m = inv.headline["metric"]
    out = []
    if inv.decompositions:
        d = inv.decompositions[0]
        if d["segments"]:
            out.append(f"Why did {d['segments'][0]['segment']} ({d['dimension']}) change so much?")
    hr = [r for r in inv.related if (r["domain"] or "") == "hr"]
    if hr:
        out.append(f"Is employee attrition associated with the {m.lower()} change?")
    out += [f"Which customers contributed most to the {m.lower()} change?", f"How does {m.lower()} compare with the same period last year?", f"What is the {m.lower()} trend over the last 12 months?"]
    return out[:5]


def template_answer(plan: QueryPlan, inv: Investigation | None, toolbox: ToolBox, db: Session, ctx) -> str:
    """Deterministic Markdown rendering used when no LLM is configured or it fails. Clearly labelled."""
    if inv is None:
        metric = db.get(Metric, plan.metric_id) if plan.metric_id else None
        if metric is not None and not metric.is_computable:
            # A definition-only import: explain what it means, and be explicit that no value exists here.
            lines = [
                "## Definition",
                f"**{metric.display_name or metric.name}** is a governed measure imported from "
                f"{metric.source_system}. {metric.description or ''}".strip(),
                "",
                f"Its native {(metric.native_language or '').upper()} formula is:",
                "",
                "```",
                metric.native_expression or metric.expression,
                "```",
                "",
                "## No value can be computed",
                "This formula has no exact SQL equivalent in this platform, so a number is deliberately "
                "**not** produced for it — an approximation here would be misleading. Use the source BI "
                "report for the figure itself, or define an equivalent governed metric.",
            ]
            alts = [m for m in db.scalars(select(Metric).where(Metric.is_computable)).all()
                    if m.table_id == metric.table_id][:5]
            if alts:
                lines += ["", "## Computable metrics on the same table",
                          *[f"- **{m.display_name or m.name}** — `{m.expression}`" for m in alts]]
            lines += ["", "## Confidence", "High for the definition; no value was estimated."]
            return "\n".join(lines)
        found = toolbox.t_find_relevant_datasets(plan.question, 6)["tables"]
        lines = ["_No AI provider is configured; showing catalog search results instead of a narrative answer._", "", "**Potentially relevant tables**", ""]
        lines += [f"- `{t['table']}` — {t['source']} · {t['domain'] or 'n/a'} · {t['rows'] or '?'} rows" for t in found] or ["- nothing matched"]
        return "\n".join(lines)
    h = inv.headline
    unit = h.get("unit") or ""
    ch = h["change_pct"]
    s = [f"_Deterministic analysis (no LLM narrative). Every figure carries a query reference._", "", "## Executive Summary"]
    if ch is None:
        s.append(f"{h['metric']} could not be computed for both periods. " + ("; ".join(inv.warnings) if inv.warnings else ""))
    else:
        s.append(f"{h['metric']} {'decreased' if ch < 0 else 'increased'} **{abs(ch):.1f}%** from {unit} {h['before']:,.0f} in {inv.comparison.label} to {unit} {h['after']:,.0f} in {inv.period.label} [{', '.join(h['query_refs'])}].")
    s += ["", "## Key Findings"]
    for i, e in enumerate(inv.evidence, 1):
        s.append(f"{i}. {e.statement} [{', '.join(e.query_refs) or 'catalog'}] — _{e.kind.replace('_', ' ')}, {e.strength} evidence_")
    if inv.related:
        s += ["", "## Cross-Domain Evidence"]
        for r in inv.related:
            if r["change_pct"] is not None:
                s.append(f"- **{r['domain'] or 'n/a'}** · {r['metric']}: {r['before']:,.1f} → {r['after']:,.1f} ({r['change_pct']:+.1f}%) via {r['path']} [{', '.join(r['query_refs'])}]")
    s += ["", "## Likely Contributors", "Evidence-supported: " + ("; ".join(e.statement for e in inv.evidence if e.strength == "strong" and e.kind != "data_quality") or "none"), "Potential (correlation only): " + ("; ".join(e.statement for e in inv.evidence if e.kind == "correlation") or "none"), "Insufficient evidence: " + ("; ".join(inv.warnings) or "none")]
    s += ["", "## Data Sources", ", ".join(inv.sources), "", "## Confidence", confidence(inv, "")["level"].title() + " — " + "; ".join(confidence(inv, "")["reasons"])]
    return "\n".join(s)
