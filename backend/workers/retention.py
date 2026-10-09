"""Data retention.

Three stores accumulate personal data as a side effect of normal operation and none of them had
a limit:

* `query_log.result_preview` — up to 20 result rows per query, masked for the *executing* user.
  An admin has no masking, so an admin's previews are unmasked names, emails and salaries.
* `anon_tokens` — the de-anonymisation key material. It must not outlive the answer it was
  created for; a vault kept forever is a permanent re-identification table.
* `audit_logs` — operational record, kept longer, but not forever.

GDPR Art. 5(1)(c) data minimisation and 5(1)(e) storage limitation.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy import null as sa_null
from sqlalchemy.orm import Session

from backend.core.config import settings
from backend.metadata.models import AnonJob, AnonToken, AuditLog, PendingAuth, QueryLog, now

log = logging.getLogger(__name__)


def purge_retention(db: Session) -> dict[str, int]:
    """Apply every retention limit. Idempotent, safe to run on a timer."""
    out: dict[str, int] = {}

    # 1. Strip stored result rows. The query itself, its ref and its timing are kept — only the
    #    DATA is dropped, so the audit trail survives without the personal information.
    cutoff = now() - timedelta(days=settings.query_preview_retention_days)
    out["query_previews_cleared"] = (
        db.query(QueryLog)
        .filter(QueryLog.created_at < cutoff, QueryLog.result_preview.isnot(None))
        # sa.null() forces SQL NULL. Assigning Python None to a JSON column serialises to the
        # JSON string 'null', which is NOT NULL in SQL — so the filter above would re-match
        # every already-purged row on every run, forever.
        .update({"result_preview": sa_null()}, synchronize_session=False)
    )

    # 2. Destroy de-anonymisation key material. After this the stored answer can no longer be
    #    mapped back to real values, which is the point.
    vault_cutoff = now() - timedelta(hours=settings.anon_vault_retention_hours)
    stale_jobs = select(AnonJob.id).where(AnonJob.created_at < vault_cutoff,
                                          AnonJob.purged_at.is_(None))
    ids = [r for r in db.scalars(stale_jobs).all()]
    if ids:
        out["anon_tokens_purged"] = (
            db.query(AnonToken).filter(AnonToken.job_id.in_(ids))
            .delete(synchronize_session=False))
        db.query(AnonJob).filter(AnonJob.id.in_(ids)).update({"purged_at": now()},
                                                             synchronize_session=False)
    out["anon_jobs_purged"] = len(ids)

    # 3. Expired interactive auth handshakes. Previously dead code that was never called.
    out["pending_auth_expired"] = (
        db.query(PendingAuth).filter(PendingAuth.expires_at < now())
        .delete(synchronize_session=False))

    # 4. Audit log, kept longest.
    audit_cutoff = now() - timedelta(days=settings.audit_retention_days)
    out["audit_rows_deleted"] = (
        db.query(AuditLog).filter(AuditLog.created_at < audit_cutoff)
        .delete(synchronize_session=False))

    db.commit()
    if any(out.values()):
        log.info("retention purge: %s", out)
    return out
