"""AI benchmark: a question set with expectations on the generated plan, the queries executed and the
answer content. The deterministic layers (planner, investigation, template answer) are always tested;
the LLM narrative is additionally checked when a provider is configured (EDI_TEST_LLM=1)."""
from __future__ import annotations

import os
import re

import pytest
from sqlalchemy import select

from backend.ai import orchestrator
from backend.ai.planner import plan_question
from backend.analytics.insights import data_anchor
from backend.metadata.models import AIProviderConfig, Conversation

pytestmark = pytest.mark.usefixtures("demo_sources", "demo_metrics")

BENCHMARK = [
    # question, expected intent, expected metric, substrings expected in the deterministic answer
    ("What is revenue?", "descriptive", "revenue", ["Revenue", "[Q-"]),
    ("Why did revenue decline in August?", "diagnostic", "revenue", ["West", "Sales Headcount", "Product Availability", "Support Tickets", "Correlation"]),
    ("Which region caused the revenue decline in August?", "segmentation", "revenue", ["West"]),
    ("Is attrition associated with the revenue decline in August?", "diagnostic", "revenue", ["Sales Headcount"]),
    ("What changed compared with last year for revenue in August?", "comparative", "revenue", ["August 2025"]),
    ("How many support tickets were there in August 2026?", "descriptive", "support_tickets", ["Support Tickets"]),
]


@pytest.mark.parametrize("question,intent,metric,expected", BENCHMARK)
def test_benchmark_deterministic(db, admin, question, intent, metric, expected):
    db.query(AIProviderConfig).delete()
    db.commit()
    plan = plan_question(db, question, data_anchor(db))
    assert plan.intent == intent, plan.to_dict()
    assert plan.metric == metric
    conv = Conversation(user_id=admin.id)
    db.add(conv)
    db.commit()
    events = list(orchestrator.run(db, admin, conv, question))
    done = events[-1]
    assert done["type"] == "done"
    text = done["content"]
    for s in expected:
        assert s in text, f"expected {s!r} in answer for {question!r}:\n{text[:800]}"
    # every query executed is read-only and logged with a ref
    assert all(re.match(r"Q-[0-9A-F]{8}", q["ref"]) for q in done["payload"]["queries"])
    assert all(q["sql"].lstrip().upper().startswith(("SELECT", "WITH")) for q in done["payload"]["queries"])


def test_no_fabrication_when_data_missing(db, admin):
    """A metric the catalog cannot compute must yield 'unavailable', never a number."""
    from backend.metadata.models import Metric, Table

    db.query(AIProviderConfig).delete()
    t = db.scalar(select(Table).where(Table.table_name == "sales"))
    m = Metric(name="phantom", display_name="Phantom Metric", table_id=t.id, expression="SUM(does_not_exist)", date_column="order_date")
    db.add(m)
    db.commit()
    conv = Conversation(user_id=admin.id)
    db.add(conv)
    db.commit()
    done = list(orchestrator.run(db, admin, conv, "Why did phantom metric drop in August 2026?"))[-1]
    assert "could not be computed" in done["content"].lower() or "unavailable" in done["content"].lower()
    assert done["payload"]["confidence"]["level"] == "low"
    db.delete(m)
    db.commit()


@pytest.mark.skipif(not os.environ.get("EDI_TEST_LLM"), reason="set EDI_TEST_LLM=1 with an AI provider configured (e.g. local Ollama)")
def test_benchmark_llm_narrative(db, admin):
    import httpx

    tags = httpx.get("http://localhost:11434/api/tags", timeout=3).json()["models"]
    model = next((m["name"] for m in tags if m["name"].startswith("qwen3")), tags[0]["name"])
    db.query(AIProviderConfig).delete()
    db.add(AIProviderConfig(name="ollama", provider="ollama", model=model, base_url="http://localhost:11434/v1", purposes=["reasoning", "planning"], is_default=True, extra={"num_ctx": 24576}))
    db.commit()
    conv = Conversation(user_id=admin.id)
    db.add(conv)
    db.commit()
    done = list(orchestrator.run(db, admin, conv, "Why did revenue decline in August?"))[-1]
    text = done["content"]
    assert "## Executive Summary" in text and "West" in text
    known = {q["ref"] for q in done["payload"]["queries"]}
    verified = set(re.findall(r"(Q-[0-9A-F]{8})(?!⚠)", text))  # refs the validator left unmarked
    assert verified and verified <= known, "every unmarked citation must correspond to an executed query"
    unverified = re.findall(r"(Q-[0-9A-Fa-f]{6,8})⚠unverified", text)
    if unverified:  # a hallucinated ref must be flagged in the narrative, in warnings and in confidence
        assert any("unverified" in w for w in done["payload"]["warnings"])
        assert done["payload"]["confidence"]["level"] != "high"
    assert not re.search(r"\bcaus(ed|es)\b.*\bcorrelat", text.lower()) or "not evidence of causation" in text.lower()
