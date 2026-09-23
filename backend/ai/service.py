"""Resolves the configured AI provider for a purpose and records usage/cost."""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.ai.providers import AIProvider, ChatResponse, make_provider
from backend.core.crypto import decrypt
from backend.metadata.models import AIProviderConfig, AIUsage

log = logging.getLogger(__name__)
PURPOSES = ["metadata", "embeddings", "planning", "reasoning", "summarization"]


def get_provider_config(db: Session, purpose: str = "reasoning") -> AIProviderConfig | None:
    cfgs = db.scalars(select(AIProviderConfig)).all()
    for c in cfgs:
        if purpose in (c.purposes or []):
            return c
    for c in cfgs:
        if c.is_default:
            return c
    return cfgs[0] if cfgs else None


def provider_from_config(cfg: AIProviderConfig) -> AIProvider:
    return make_provider(cfg.provider, cfg.model, decrypt(cfg.api_key) if cfg.api_key else None, cfg.base_url, cfg.temperature, cfg.max_tokens, cfg.embedding_model, cfg.extra)


def get_provider(db: Session, purpose: str = "reasoning") -> tuple[AIProvider, AIProviderConfig] | None:
    cfg = get_provider_config(db, purpose)
    if cfg is None:
        return None
    return provider_from_config(cfg), cfg


def embed_fn(db: Session):
    """Return a callable(list[str]) -> vectors, or None if no embedding model configured."""
    got = get_provider(db, "embeddings")
    if not got or not got[1].embedding_model:
        return None, None
    prov, cfg = got
    return prov.generate_embedding, cfg.embedding_model


def record_usage(db: Session, cfg: AIProviderConfig, resp: ChatResponse, purpose: str, user_id: str | None, conversation_id: str | None = None, question: str | None = None) -> AIUsage:
    cost = (resp.input_tokens * cfg.input_cost_per_1m + resp.output_tokens * cfg.output_cost_per_1m) / 1_000_000
    u = AIUsage(user_id=user_id, conversation_id=conversation_id, provider=cfg.provider, model=cfg.model, purpose=purpose, input_tokens=resp.input_tokens, output_tokens=resp.output_tokens, estimated_cost=round(cost, 6), duration_ms=resp.duration_ms, question=(question or "")[:500] or None)
    db.add(u)
    db.flush()
    return u


class UsageTracker:
    """Accumulates usage across an orchestration run."""

    def __init__(self, db: Session, cfg: AIProviderConfig | None, user_id: str | None, conversation_id: str | None, question: str | None):
        self.db, self.cfg, self.user_id, self.conversation_id, self.question = db, cfg, user_id, conversation_id, question
        self.input_tokens = self.output_tokens = self.calls = 0
        self.cost = 0.0

    def add(self, resp: ChatResponse, purpose: str) -> None:
        if self.cfg is None:
            return
        u = record_usage(self.db, self.cfg, resp, purpose, self.user_id, self.conversation_id, self.question)
        self.input_tokens += resp.input_tokens
        self.output_tokens += resp.output_tokens
        self.cost += u.estimated_cost
        self.calls += 1

    def summary(self) -> dict[str, Any]:
        return {"provider": self.cfg.provider if self.cfg else None, "model": self.cfg.model if self.cfg else None, "calls": self.calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens, "estimated_cost": round(self.cost, 6)}
