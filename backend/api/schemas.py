"""Pydantic request/response models shared by routers."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# auth
class LoginIn(BaseModel):
    email: str
    password: str


class UserOut(ORM):
    id: str
    email: str
    name: str
    role: str
    department: str | None = None
    is_active: bool


class UserIn(BaseModel):
    email: EmailStr
    name: str
    password: str = Field(min_length=6)
    role: str = "analyst"
    department: str | None = None


class UserUpdate(BaseModel):
    name: str | None = None
    role: str | None = None
    department: str | None = None
    is_active: bool | None = None
    password: str | None = None


# sources
class SourceIn(BaseModel):
    name: str
    type: str
    config: dict[str, Any] = {}
    owner: str | None = None
    department: str | None = None
    description: str | None = None
    refresh_frequency: str = "manual"
    read_only: bool = True
    is_enabled: bool = True


class SourceUpdate(BaseModel):
    name: str | None = None
    config: dict[str, Any] | None = None
    owner: str | None = None
    department: str | None = None
    description: str | None = None
    refresh_frequency: str | None = None
    read_only: bool | None = None
    is_enabled: bool | None = None


class SourceOut(ORM):
    id: str
    name: str
    type: str
    config: dict[str, Any]
    owner: str | None
    department: str | None
    description: str | None
    refresh_frequency: str
    status: str
    is_enabled: bool
    read_only: bool
    last_sync_at: datetime | None
    last_error: str | None
    health: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    table_count: int = 0
    freshness: str | None = None


# catalog
class ColumnOut(ORM):
    id: str
    table_id: str
    column_name: str
    ordinal: int
    data_type: str
    logical_type: str
    business_name: str | None
    description: str | None
    business_definition: str | None
    semantic_type: str | None
    unit: str | None
    is_primary_key: bool
    is_foreign_key: bool
    is_nullable: bool
    is_sensitive: bool
    sensitivity: str
    pii_type: str | None
    sample_values: list
    stats: dict[str, Any]
    ai_suggestion: dict[str, Any] | None


class ColumnUpdate(BaseModel):
    business_name: str | None = None
    description: str | None = None
    business_definition: str | None = None
    semantic_type: str | None = None
    unit: str | None = None
    sensitivity: str | None = None
    pii_type: str | None = None
    is_sensitive: bool | None = None


class TableOut(ORM):
    id: str
    dataset_id: str
    schema_name: str | None
    table_name: str
    business_name: str | None
    description: str | None
    business_domain: str | None
    row_count: int | None
    column_count: int | None
    last_updated: datetime | None
    last_profiled_at: datetime | None
    date_column: str | None
    ai_suggestion: dict[str, Any] | None
    tags: list
    source_name: str | None = None
    source_id: str | None = None
    dataset_name: str | None = None
    open_issues: int = 0
    relationship_count: int = 0


class TableUpdate(BaseModel):
    business_name: str | None = None
    description: str | None = None
    business_domain: str | None = None
    date_column: str | None = None
    tags: list[str] | None = None


class DatasetOut(ORM):
    id: str
    source_id: str
    name: str
    description: str | None
    business_domain: str | None
    owner: str | None
    sensitivity: str
    refresh_frequency: str | None
    source_name: str | None = None
    source_type: str | None = None
    table_count: int = 0


class DatasetUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    business_domain: str | None = None
    owner: str | None = None
    sensitivity: str | None = None


# semantic
class MetricIn(BaseModel):
    name: str
    display_name: str | None = None
    description: str | None = None
    table_id: str | None = None
    expression: str
    filters: str | None = None
    date_column: str | None = None
    unit: str | None = None
    format: str = "number"
    domain: str | None = None
    owner: str | None = None
    direction: str = "up"
    related_metrics: list[str] = []
    dimensions: list[str] = []


class MetricUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    table_id: str | None = None
    expression: str | None = None
    filters: str | None = None
    date_column: str | None = None
    unit: str | None = None
    format: str | None = None
    domain: str | None = None
    owner: str | None = None
    direction: str | None = None
    related_metrics: list[str] | None = None
    dimensions: list[str] | None = None


class MetricOut(ORM):
    id: str
    name: str
    display_name: str | None
    description: str | None
    table_id: str | None
    expression: str
    filters: str | None
    date_column: str | None
    unit: str | None
    format: str
    domain: str | None
    owner: str | None
    direction: str
    related_metrics: list
    dimensions: list
    version: int
    updated_at: datetime
    native_expression: str | None = None
    native_language: str | None = None
    is_computable: bool = True
    source_system: str | None = None
    table_name: str | None = None
    source_name: str | None = None


class GlossaryIn(BaseModel):
    term: str
    definition: str
    domain: str | None = None
    owner: str | None = None
    synonyms: list[str] = []
    related_terms: list[str] = []
    rules: list[str] = []


class GlossaryOut(ORM):
    id: str
    term: str
    definition: str
    domain: str | None
    owner: str | None
    synonyms: list
    related_terms: list
    rules: list
    updated_at: datetime


class EntityIn(BaseModel):
    name: str
    description: str | None = None
    domain: str | None = None


class EntityMappingIn(BaseModel):
    column_id: str
    role: str = "key"
    confidence: float = 1.0


class RelationshipIn(BaseModel):
    from_column_id: str
    to_column_id: str
    type: str = "many_to_one"
    reason: str | None = None


class RelationshipOut(BaseModel):
    id: str
    from_column_id: str
    to_column_id: str
    from_table: str
    from_column: str
    from_source: str
    to_table: str
    to_column: str
    to_source: str
    type: str
    confidence: float
    reason: str | None
    status: str
    is_cross_source: bool
    created_by: str
    evidence: dict[str, Any]


class PermissionIn(BaseModel):
    role: str
    resource_type: str
    resource_id: str
    access: str = "allow"
    row_filter: str | None = None
    mask_columns: bool = True


class AIProviderIn(BaseModel):
    name: str
    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    temperature: float = 0.1
    max_tokens: int = 4096
    embedding_model: str | None = None
    purposes: list[str] = []
    is_default: bool = False
    input_cost_per_1m: float = 0.0
    output_cost_per_1m: float = 0.0
    extra: dict[str, Any] = {}


class AIProviderOut(ORM):
    id: str
    name: str
    provider: str
    model: str
    api_key: str | None
    base_url: str | None
    temperature: float
    max_tokens: int
    embedding_model: str | None
    purposes: list
    is_default: bool
    input_cost_per_1m: float
    output_cost_per_1m: float
    extra: dict[str, Any]


class AskIn(BaseModel):
    question: str
    conversation_id: str | None = None


class SQLIn(BaseModel):
    source_id: str
    sql: str
    limit: int = 200
