"""Anonymisation policy: what the AI is allowed to see, per column.

Distinct from `/admin/permissions`, which controls what *people* see. This controls what leaves
the machine. An admin can be entitled to view every row and still have the model see only tokens.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from backend.anonymize import Anonymizer, new_job
from backend.anonymize.policy import Policy, default_strategy_for, policy_for
from backend.anonymize.strategies import ENTITY_CODES, STRATEGIES
from backend.audit.service import audit
from backend.core.db import get_db
from backend.metadata.models import AnonJob, AnonToken, Column, Table, User
from backend.metadata.pii import mask_value
from backend.security.auth import require_role

router = APIRouter(prefix="/privacy", tags=["privacy"])


class PolicyOut(BaseModel):
    column_id: str
    table_id: str
    table_name: str
    source_name: str | None = None
    column_name: str
    logical_type: str
    pii_type: str | None = None
    sensitivity: str
    strategy: str
    entity: str
    params: dict = Field(default_factory=dict)
    is_default: bool          # False once a human has chosen explicitly
    suggested: str            # what detection proposes
    accepted_by: str | None = None
    sample: str | None = None  # masked — a privacy screen must not display the data it protects
    preview: str | None = None  # exactly what the model would receive for that sample


class PolicyIn(BaseModel):
    strategy: str
    entity: str = "other"
    params: dict = Field(default_factory=dict)


def _preview(db: Session, col: Column, table_name: str, strategy: str, entity: str, params: dict) -> tuple[str | None, str | None]:
    """Render one real sample through the policy, so the UI can show what actually goes out.

    The preview must be produced by the same code path as a live request — a preview that is
    merely a plausible-looking string would be worse than showing nothing.
    """
    samples = [s for s in (col.sample_values or []) if s not in (None, "")]
    if not samples:
        return None, None
    raw = str(samples[0])
    policy = Policy(table_name.lower(), col.column_name.lower(), strategy, entity, params)
    job = AnonJob(salt="preview", id="preview")
    # persist=False: an in-memory vault that writes nothing and never touches `db`'s identity
    # map. It still runs the real _apply(), so the preview cannot drift from live behaviour.
    anon = Anonymizer(None, job, {(policy.table, policy.column): policy}, persist=False)
    try:
        rendered = anon._apply(policy, raw)
    except Exception:  # noqa: BLE001 - a bad param set must not break the page
        rendered = None
    return mask_value(raw, col.pii_type or "name"), (str(rendered) if rendered is not None else None)


@router.get("/policies", response_model=list[PolicyOut])
def list_policies(
    table_id: str | None = None,
    only_protected: bool = False,
    db: Session = Depends(get_db),
    # Not get_current_user: for a passthrough column the preview shows the REAL value, since
    # that is exactly what would be sent. A viewer has PII masked everywhere else and must not
    # be handed it back through this screen.
    user: User = Depends(require_role("analyst")),
):
    q = select(Table).options(selectinload(Table.columns))
    if table_id:
        q = q.where(Table.id == table_id)
    out: list[PolicyOut] = []
    for t in db.scalars(q).all():
        name = t.qualified_name or t.table_name
        for col in sorted(t.columns, key=lambda c: c.ordinal):
            eff = policy_for(col, name)
            if only_protected and eff.strategy == "passthrough":
                continue
            sample, preview = _preview(db, col, name, eff.strategy, eff.entity, eff.params)
            out.append(PolicyOut(
                column_id=col.id, table_id=t.id, table_name=name,
                source_name=t.dataset.source.name if t.dataset and t.dataset.source else None,
                column_name=col.column_name, logical_type=col.logical_type,
                pii_type=col.pii_type, sensitivity=col.sensitivity,
                strategy=eff.strategy, entity=eff.entity, params=eff.params,
                is_default=col.anon_strategy is None,
                suggested=default_strategy_for(col)[0],
                accepted_by=col.anon_accepted_by, sample=sample, preview=preview,
            ))
    return out


@router.get("/summary")
def summary(db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    """Counts for the dashboard banner, plus the columns worth a human's attention."""
    cols = db.scalars(select(Column).options(selectinload(Column.table))).all()
    protected = unreviewed = 0
    unprotected_pii: list[dict] = []
    for col in cols:
        table_name = col.table.qualified_name or col.table.table_name if col.table else ""
        eff = policy_for(col, table_name)
        if eff.strategy != "passthrough":
            protected += 1
            if col.anon_strategy is None:
                unreviewed += 1
        elif col.pii_type or col.sensitivity in {"pii", "restricted"}:
            # Detected as sensitive but passing through: either a deliberate override or an
            # oversight. Either way the operator should see it.
            unprotected_pii.append({"column_id": col.id, "table": table_name,
                                    "column": col.column_name, "pii_type": col.pii_type})
    return {
        "columns_total": len(cols),
        "columns_protected": protected,
        "protected_by_default_not_yet_reviewed": unreviewed,
        "unprotected_detected_pii": unprotected_pii,
        "strategies": list(STRATEGIES),
        "entities": list(ENTITY_CODES),
    }


@router.put("/policies/{column_id}", response_model=PolicyOut)
def set_policy(
    column_id: str,
    body: PolicyIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("analyst")),
):
    if body.strategy not in STRATEGIES:
        raise HTTPException(400, f"unknown strategy '{body.strategy}'; expected one of {list(STRATEGIES)}")
    if body.entity not in ENTITY_CODES:
        raise HTTPException(400, f"unknown entity '{body.entity}'; expected one of {list(ENTITY_CODES)}")
    col = db.get(Column, column_id)
    if col is None:
        raise HTTPException(404, "column not found")
    before = col.anon_strategy
    col.anon_strategy, col.anon_entity, col.anon_params = body.strategy, body.entity, body.params
    col.anon_accepted_by = user.email
    db.commit()
    # Weakening protection is a security-relevant act and is audited as one.
    audit(db, user, "privacy.policy_changed", "column", col.id,
          {"column": col.column_name, "from": before, "to": body.strategy,
           "weakened": body.strategy == "passthrough" and (before or "") != "passthrough"})
    t = col.table
    name = t.qualified_name or t.table_name
    sample, preview = _preview(db, col, name, body.strategy, body.entity, body.params)
    return PolicyOut(
        column_id=col.id, table_id=t.id, table_name=name,
        source_name=t.dataset.source.name if t.dataset and t.dataset.source else None,
        column_name=col.column_name, logical_type=col.logical_type, pii_type=col.pii_type,
        sensitivity=col.sensitivity, strategy=body.strategy, entity=body.entity,
        params=body.params, is_default=False, suggested=default_strategy_for(col)[0],
        accepted_by=col.anon_accepted_by, sample=sample, preview=preview,
    )


@router.post("/policies/accept-suggested")
def accept_suggested(
    table_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_role("analyst")),
):
    """Make the detected defaults explicit, so they stop being 'unreviewed'."""
    q = select(Column).options(selectinload(Column.table))
    if table_id:
        q = q.where(Column.table_id == table_id)
    n = 0
    for col in db.scalars(q).all():
        strategy, entity = default_strategy_for(col)
        if col.anon_strategy is None and strategy != "passthrough":
            col.anon_strategy, col.anon_entity, col.anon_accepted_by = strategy, entity, user.email
            n += 1
    db.commit()
    audit(db, user, "privacy.accept_suggested", "table", table_id, {"columns": n})
    return {"accepted": n}


@router.delete("/vault/{job_id}", status_code=204)
def purge_job(job_id: str, db: Session = Depends(get_db), user: User = Depends(require_role("admin"))):
    """Destroy a job's token mappings. After this the answer can no longer be de-anonymised."""
    job = db.get(AnonJob, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    db.query(AnonToken).filter(AnonToken.job_id == job_id).delete()
    from backend.metadata.models import now

    job.purged_at = now()
    db.commit()
    audit(db, user, "privacy.vault_purged", "anon_job", job_id, {})
