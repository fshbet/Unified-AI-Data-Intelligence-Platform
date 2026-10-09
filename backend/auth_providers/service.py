"""Credential lifecycle: the only way anything in the app obtains a credential.

Everything funnels through `get_credential`, which decrypts, refreshes when expired, re-encrypts
and stores. Centralising it is what makes "ten parallel table syncs cause one token refresh"
true, and what lets a failed refresh mark the source `needs_auth` in exactly one place.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from backend.auth_providers.base import AuthProvider, Credential, InteractiveStart, create_auth
from backend.core.crypto import decrypt, encrypt
from backend.metadata.models import DataSource, PendingAuth, SourceCredential, now

log = logging.getLogger(__name__)

# One lock per source. Without it a 12-table sync fires 12 simultaneous refreshes, and providers
# that rotate the refresh token on use will invalidate each other's.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class AuthError(RuntimeError):
    pass


def _lock_for(source_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(source_id, threading.Lock())


def provider_for(source: DataSource) -> AuthProvider:
    from backend.core.crypto import decrypt_config

    return create_auth(source.auth_mode or "credentials", decrypt_config(source.auth_config or {}))


# ---------------------------------------------------------------------------- storage
def _store(db: Session, source_id: str, cred: Credential) -> None:
    row = db.get(SourceCredential, source_id) or SourceCredential(source_id=source_id)
    row.kind = cred.kind
    row.secret_enc = encrypt(cred.secret or "")
    row.refresh_enc = encrypt(cred.refresh_token) if cred.refresh_token else None
    row.username = cred.username
    row.expires_at = cred.expires_at
    row.scopes = list(cred.scopes)
    row.meta = cred.metadata
    row.updated_at = now()
    db.add(row)
    db.flush()


def _load(db: Session, source_id: str) -> Credential | None:
    row = db.get(SourceCredential, source_id)
    if row is None:
        return None
    return Credential(
        kind=row.kind, secret=decrypt(row.secret_enc),
        refresh_token=decrypt(row.refresh_enc) if row.refresh_enc else None,
        username=row.username, expires_at=row.expires_at,
        scopes=tuple(row.scopes or ()), metadata=row.meta or {},
    )


# ---------------------------------------------------------------------------- the api
def get_credential(db: Session, source: DataSource) -> Credential:
    """Decrypt, refresh if needed, persist, return. The only entry point.

    Commits when it refreshes. That is deliberate and it is what makes the lock actually work:
    the lock serialises the refresh, but each waiting caller has its own Session, so unless the
    new token is committed the next one in simply re-reads the stale row and refreshes again.
    With a provider that rotates refresh tokens on use, those two refreshes invalidate each
    other. A refreshed token should be durable immediately in any case — losing it to a crash
    only forces another refresh.
    """
    with _lock_for(source.id):
        # Re-read under the lock: another caller may have refreshed while we waited, and this
        # Session's identity map would still be holding the expired row.
        db.expire_all()
        cred = _load(db, source.id)

        # Non-interactive modes can always mint a fresh credential, so a missing or expired one
        # is not an error — only the interactive modes genuinely need the user back.
        auth = provider_for(source)
        if cred is None:
            if auth.interactive:
                _needs_auth(db, source, "This connection has not been authorised yet")
            cred = auth.acquire()
            _store(db, source.id, cred)
            db.commit()
            return cred

        if not cred.is_expired():
            return cred

        try:
            fresh = auth.refresh(cred)
        except Exception as exc:  # noqa: BLE001 - every failure here means the same thing
            log.warning("credential refresh failed for source %s: %s", source.id, exc)
            _needs_auth(db, source, f"Could not refresh the credential: {exc}")
        _store(db, source.id, fresh)
        db.commit()
        return fresh


def _needs_auth(db: Session, source: DataSource, message: str) -> None:
    source.status = "needs_auth"
    source.last_error = message
    db.flush()
    raise AuthError(message)


# ---------------------------------------------------------------------------- interactive
def start_interactive(db: Session, source: DataSource, redirect_uri: str | None = None) -> InteractiveStart:
    auth = provider_for(source)
    if not auth.interactive:
        raise AuthError(f"'{source.auth_mode}' does not require interactive authorisation")
    start = auth.begin(redirect_uri)
    pending = getattr(start, "_pkce", {})
    state = pending.get("state") or pending.get("device_code") or ""
    if not state:
        raise AuthError("The auth provider did not return anything to correlate the callback with")
    db.add(PendingAuth(
        state=state, source_id=source.id, mode=source.auth_mode,
        payload_enc=encrypt(json.dumps(pending)),
        # Short-lived on purpose: a stale pending row is an open door.
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=start.expires_in or 600),
    ))
    db.flush()
    return start


def complete_interactive(db: Session, source: DataSource, payload: dict, state_key: str) -> Credential:
    row = db.get(PendingAuth, state_key)
    if row is None or row.source_id != source.id:
        # Unknown state: this callback did not come from a flow we started. Refuse rather than
        # exchange the code anyway.
        raise AuthError("Unknown or already-used authorisation state")
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    expired = expires < datetime.now(timezone.utc)
    stored = json.loads(decrypt(row.payload_enc))
    # Single-use, whether or not it succeeds.
    db.delete(row)
    db.flush()
    if expired:
        raise AuthError("This authorisation request expired. Start again.")

    cred = provider_for(source).complete(payload, stored)
    _store(db, source.id, cred)
    source.status = "connected"
    source.last_error = None
    db.flush()
    return cred


def poll_interactive(db: Session, source: DataSource, state_key: str) -> Credential:
    """Device flow. Raises AuthPending (from the provider) while the user has not finished."""
    row = db.get(PendingAuth, state_key)
    if row is None or row.source_id != source.id:
        raise AuthError("Unknown or already-used authorisation state")
    stored = json.loads(decrypt(row.payload_enc))
    cred = provider_for(source).complete({}, stored)  # AuthPending propagates to the caller
    db.delete(row)
    source.status = "connected"
    source.last_error = None
    _store(db, source.id, cred)
    db.flush()
    return cred


def purge_expired(db: Session) -> int:
    n = db.query(PendingAuth).filter(PendingAuth.expires_at < now()).delete()
    db.flush()
    return n
