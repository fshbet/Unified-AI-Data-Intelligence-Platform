"""Metadata catalog + semantic layer schema.

This is the central repository: sources → datasets → tables → columns, plus the semantic
layer (entities, metrics, glossary, relationships), quality, security and audit tables.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.core.db import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


# --------------------------------------------------------------------------- security
class User(Base, TimestampMixin):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="analyst")  # admin | analyst | viewer
    department: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Permission(Base, TimestampMixin):
    """Role-scoped access rule. resource_type in dataset|table|column. access allow|deny.
    row_filter is a SQL predicate appended to every query on that table for the role."""

    __tablename__ = "permissions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    role: Mapped[str] = mapped_column(String(32), index=True)
    resource_type: Mapped[str] = mapped_column(String(16))
    resource_id: Mapped[str] = mapped_column(String(32), index=True)
    access: Mapped[str] = mapped_column(String(8), default="allow")
    row_filter: Mapped[str | None] = mapped_column(Text)
    mask_columns: Mapped[bool] = mapped_column(Boolean, default=True)


# --------------------------------------------------------------------------- catalog
class DataSource(Base, TimestampMixin):
    __tablename__ = "data_sources"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    type: Mapped[str] = mapped_column(String(64), index=True)  # connector registry key
    config: Mapped[dict] = mapped_column(JSON, default=dict)  # encrypted secret fields
    owner: Mapped[str | None] = mapped_column(String(255))
    department: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    refresh_frequency: Mapped[str] = mapped_column(String(32), default="manual")  # manual|15m|hourly|daily|weekly
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|connected|imported|error|disabled
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    read_only: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    health: Mapped[dict] = mapped_column(JSON, default=dict)

    datasets: Mapped[list[Dataset]] = relationship(back_populates="source", cascade="all, delete-orphan")


class Dataset(Base, TimestampMixin):
    __tablename__ = "datasets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    business_domain: Mapped[str | None] = mapped_column(String(128), index=True)
    owner: Mapped[str | None] = mapped_column(String(255))
    sensitivity: Mapped[str] = mapped_column(String(32), default="internal")  # public|internal|sensitive|restricted
    refresh_frequency: Mapped[str | None] = mapped_column(String(32))

    source: Mapped[DataSource] = relationship(back_populates="datasets")
    tables: Mapped[list[Table]] = relationship(back_populates="dataset", cascade="all, delete-orphan")


class Table(Base, TimestampMixin):
    __tablename__ = "tables"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    schema_name: Mapped[str | None] = mapped_column(String(255))
    table_name: Mapped[str] = mapped_column(String(255), index=True)
    business_name: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    business_domain: Mapped[str | None] = mapped_column(String(128))
    row_count: Mapped[int | None] = mapped_column(Integer)
    column_count: Mapped[int | None] = mapped_column(Integer)
    last_updated: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_profiled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    date_column: Mapped[str | None] = mapped_column(String(255))  # primary time axis for period analysis
    ai_suggestion: Mapped[dict | None] = mapped_column(JSON)
    tags: Mapped[list] = mapped_column(JSON, default=list)

    dataset: Mapped[Dataset] = relationship(back_populates="tables")
    columns: Mapped[list[Column]] = relationship(
        back_populates="table", cascade="all, delete-orphan", order_by="Column.ordinal"
    )
    profiles: Mapped[list[TableProfile]] = relationship(back_populates="table", cascade="all, delete-orphan")

    @property
    def qualified_name(self) -> str:
        return f"{self.schema_name}.{self.table_name}" if self.schema_name else self.table_name


class Column(Base, TimestampMixin):
    __tablename__ = "columns"
    __table_args__ = (UniqueConstraint("table_id", "column_name"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    table_id: Mapped[str] = mapped_column(ForeignKey("tables.id", ondelete="CASCADE"), index=True)
    column_name: Mapped[str] = mapped_column(String(255))
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    data_type: Mapped[str] = mapped_column(String(128))
    logical_type: Mapped[str] = mapped_column(String(32), default="string")  # number|integer|string|date|datetime|boolean|json
    business_name: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    business_definition: Mapped[str | None] = mapped_column(Text)
    semantic_type: Mapped[str | None] = mapped_column(String(64))  # identifier|measure|dimension|date|text|...
    unit: Mapped[str | None] = mapped_column(String(32))
    is_primary_key: Mapped[bool] = mapped_column(Boolean, default=False)
    is_foreign_key: Mapped[bool] = mapped_column(Boolean, default=False)
    is_nullable: Mapped[bool] = mapped_column(Boolean, default=True)
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    sensitivity: Mapped[str] = mapped_column(String(16), default="public")  # public|sensitive|pii|restricted
    pii_type: Mapped[str | None] = mapped_column(String(32))
    sample_values: Mapped[list] = mapped_column(JSON, default=list)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)  # min/max/mean/median/std/percentiles/distinct/nulls/freq
    ai_suggestion: Mapped[dict | None] = mapped_column(JSON)

    table: Mapped[Table] = relationship(back_populates="columns")


class TableProfile(Base):
    """Snapshot history used for volume-change / freshness detection."""

    __tablename__ = "table_profiles"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    table_id: Mapped[str] = mapped_column(ForeignKey("tables.id", ondelete="CASCADE"), index=True)
    row_count: Mapped[int] = mapped_column(Integer)
    column_signature: Mapped[str] = mapped_column(Text)
    max_date: Mapped[str | None] = mapped_column(String(64))
    profiled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    table: Mapped[Table] = relationship(back_populates="profiles")


# --------------------------------------------------------------------------- semantic layer
class Entity(Base, TimestampMixin):
    __tablename__ = "entities"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(String(128))
    mappings: Mapped[list[EntityMapping]] = relationship(back_populates="entity", cascade="all, delete-orphan")


class EntityMapping(Base, TimestampMixin):
    """Maps a physical column to a canonical entity key (cross-system entity resolution)."""

    __tablename__ = "entity_mappings"
    __table_args__ = (UniqueConstraint("entity_id", "column_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    entity_id: Mapped[str] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"), index=True)
    column_id: Mapped[str] = mapped_column(ForeignKey("columns.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(32), default="key")  # key | attribute
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    transform: Mapped[str | None] = mapped_column(String(255))  # e.g. "lpad(x,4,'0')" normalisation hint
    entity: Mapped[Entity] = relationship(back_populates="mappings")
    column: Mapped[Column] = relationship()


class Relationship(Base, TimestampMixin):
    __tablename__ = "relationships"
    __table_args__ = (Index("ix_rel_cols", "from_column_id", "to_column_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    from_column_id: Mapped[str] = mapped_column(ForeignKey("columns.id", ondelete="CASCADE"))
    to_column_id: Mapped[str] = mapped_column(ForeignKey("columns.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(32), default="many_to_one")  # one_to_one|one_to_many|many_to_one|many_to_many
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="suggested", index=True)  # suggested|approved|rejected
    is_cross_source: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    from_column: Mapped[Column] = relationship(foreign_keys=[from_column_id])
    to_column: Mapped[Column] = relationship(foreign_keys=[to_column_id])


class Metric(Base, TimestampMixin):
    __tablename__ = "metrics"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    table_id: Mapped[str | None] = mapped_column(ForeignKey("tables.id", ondelete="SET NULL"), index=True)
    expression: Mapped[str] = mapped_column(Text)  # SQL aggregate expression, e.g. SUM(revenue)
    filters: Mapped[str | None] = mapped_column(Text)  # SQL predicate
    date_column: Mapped[str | None] = mapped_column(String(255))
    # Measures imported from a BI semantic model (Power BI DAX, Tableau formula) keep their original
    # definition. is_computable is False when the native formula could not be translated to SQL —
    # the metric stays discoverable and quotable, but the engine refuses to invent a number for it.
    native_expression: Mapped[str | None] = mapped_column(Text)
    native_language: Mapped[str | None] = mapped_column(String(16))  # dax | tableau
    is_computable: Mapped[bool] = mapped_column(Boolean, default=True)
    source_system: Mapped[str | None] = mapped_column(String(64))  # e.g. "Power BI · Sales Model"
    unit: Mapped[str | None] = mapped_column(String(32))
    format: Mapped[str] = mapped_column(String(16), default="number")  # number|currency|percent
    domain: Mapped[str | None] = mapped_column(String(128))
    owner: Mapped[str | None] = mapped_column(String(255))
    direction: Mapped[str] = mapped_column(String(8), default="up")  # up = higher is better
    related_metrics: Mapped[list] = mapped_column(JSON, default=list)
    dimensions: Mapped[list] = mapped_column(JSON, default=list)  # column names usable for decomposition
    version: Mapped[int] = mapped_column(Integer, default=1)
    table: Mapped[Table | None] = relationship()


class GlossaryTerm(Base, TimestampMixin):
    __tablename__ = "glossary_terms"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    term: Mapped[str] = mapped_column(String(128), unique=True)
    definition: Mapped[str] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(String(128))
    owner: Mapped[str | None] = mapped_column(String(255))
    synonyms: Mapped[list] = mapped_column(JSON, default=list)
    related_terms: Mapped[list] = mapped_column(JSON, default=list)
    rules: Mapped[list] = mapped_column(JSON, default=list)  # business rules, free text / SQL snippets


class SemanticVersion(Base):
    __tablename__ = "semantic_versions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    object_type: Mapped[str] = mapped_column(String(32), index=True)
    object_id: Mapped[str] = mapped_column(String(32), index=True)
    object_name: Mapped[str] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(Integer)
    changed_by: Mapped[str] = mapped_column(String(255))
    change_summary: Mapped[str] = mapped_column(Text)
    previous: Mapped[dict | None] = mapped_column(JSON)
    current: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Embedding(Base):
    """Pluggable vector store default: vectors in the catalog DB, cosine search in numpy.
    ponytail: fine up to ~100k objects; swap VectorStore impl for pgvector/qdrant beyond that."""

    __tablename__ = "embeddings"
    __table_args__ = (UniqueConstraint("object_type", "object_id"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    object_type: Mapped[str] = mapped_column(String(32), index=True)
    object_id: Mapped[str] = mapped_column(String(32), index=True)
    text: Mapped[str] = mapped_column(Text)
    vector: Mapped[list | None] = mapped_column(JSON)
    model: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


# --------------------------------------------------------------------------- quality & insights
class DataQualityIssue(Base):
    __tablename__ = "data_quality_issues"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    table_id: Mapped[str | None] = mapped_column(ForeignKey("tables.id", ondelete="CASCADE"), index=True)
    column_id: Mapped[str | None] = mapped_column(ForeignKey("columns.id", ondelete="CASCADE"))
    rule: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="warning")  # info|warning|critical
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|acknowledged|resolved
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    table: Mapped[Table | None] = relationship()


class Insight(Base):
    __tablename__ = "insights"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(16), default="info")
    kind: Mapped[str] = mapped_column(String(32), default="anomaly")  # anomaly|trend|quality
    metric_id: Mapped[str | None] = mapped_column(String(32))
    period: Mapped[str | None] = mapped_column(String(32))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="new")  # new|investigating|dismissed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


# --------------------------------------------------------------------------- AI
class AIProviderConfig(Base, TimestampMixin):
    __tablename__ = "ai_provider_configs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    provider: Mapped[str] = mapped_column(String(32))  # openai|anthropic|gemini|azure_openai|ollama|openrouter|custom
    model: Mapped[str] = mapped_column(String(128))
    api_key: Mapped[str | None] = mapped_column(Text)  # encrypted
    base_url: Mapped[str | None] = mapped_column(String(512))
    temperature: Mapped[float] = mapped_column(Float, default=0.1)
    max_tokens: Mapped[int] = mapped_column(Integer, default=4096)
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    purposes: Mapped[list] = mapped_column(JSON, default=list)  # metadata|embeddings|planning|reasoning|summarization
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    input_cost_per_1m: Mapped[float] = mapped_column(Float, default=0.0)
    output_cost_per_1m: Mapped[float] = mapped_column(Float, default=0.0)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(255), default="New analysis")
    context: Mapped[dict] = mapped_column(JSON, default=dict)  # analytical context carried between turns
    is_saved: Mapped[bool] = mapped_column(Boolean, default=False)
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at"
    )


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user|assistant
    content: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)  # plan, evidence, queries, charts, sources, confidence
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class QueryLog(Base):
    __tablename__ = "query_log"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ref: Mapped[str] = mapped_column(String(16), unique=True)  # Q-000123
    user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    source_id: Mapped[str | None] = mapped_column(String(32), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(32), index=True)
    sql: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16))  # ok|error|blocked
    row_count: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    result_preview: Mapped[list | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class AIUsage(Base):
    __tablename__ = "ai_usage"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    purpose: Mapped[str] = mapped_column(String(32))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    question: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    user_email: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource_type: Mapped[str | None] = mapped_column(String(32))
    resource_id: Mapped[str | None] = mapped_column(String(32))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class Job(Base):
    """Background job record (schema refresh, profiling, AI description generation...)."""

    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    target_id: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|done|error
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
