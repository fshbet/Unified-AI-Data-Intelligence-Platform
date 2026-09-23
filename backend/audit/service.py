"""Audit log helper."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from backend.metadata.models import AuditLog, User


def audit(db: Session, user: User | None, action: str, resource_type: str | None = None, resource_id: str | None = None, details: dict[str, Any] | None = None, ip: str | None = None) -> AuditLog:
    row = AuditLog(user_id=user.id if user else None, user_email=user.email if user else None, action=action, resource_type=resource_type, resource_id=resource_id, details=details or {}, ip=ip)
    db.add(row)
    db.flush()
    return row
