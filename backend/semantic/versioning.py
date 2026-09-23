"""Semantic model versioning: every change to a metric/column definition/relationship/glossary term
records a version row with before/after snapshots."""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.metadata.models import SemanticVersion

TRACKED_FIELDS = {
    "metric": ["name", "display_name", "description", "expression", "filters", "date_column", "unit", "format", "domain", "owner", "direction", "related_metrics", "dimensions", "table_id", "native_expression", "native_language", "is_computable", "source_system"],
    "column": ["business_name", "description", "business_definition", "semantic_type", "unit", "sensitivity", "pii_type", "is_sensitive"],
    "relationship": ["type", "status", "confidence", "reason"],
    "glossary": ["term", "definition", "domain", "owner", "synonyms", "related_terms", "rules"],
    "table": ["business_name", "description", "business_domain", "date_column", "tags"],
    "entity": ["name", "description", "domain"],
}


def snapshot(obj: Any, object_type: str) -> dict:
    return {f: getattr(obj, f, None) for f in TRACKED_FIELDS[object_type]}


def record_version(db: Session, object_type: str, obj: Any, previous: dict | None, changed_by: str, name: str | None = None) -> SemanticVersion | None:
    current = snapshot(obj, object_type)
    if previous is not None:
        changed = {k for k in current if current[k] != previous.get(k)}
        if not changed:
            return None
        summary = "Changed " + ", ".join(sorted(changed))
    else:
        summary = "Created"
    last = db.scalar(select(func.max(SemanticVersion.version)).where(SemanticVersion.object_type == object_type, SemanticVersion.object_id == obj.id)) or 0
    v = SemanticVersion(object_type=object_type, object_id=obj.id, object_name=name or getattr(obj, "name", None) or getattr(obj, "term", None) or getattr(obj, "column_name", None) or obj.id,
                        version=last + 1, changed_by=changed_by, change_summary=summary, previous=previous, current=current)
    db.add(v)
    if hasattr(obj, "version"):
        obj.version = last + 1
    db.flush()
    return v
