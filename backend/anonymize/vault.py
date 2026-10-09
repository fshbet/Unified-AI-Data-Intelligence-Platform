"""Job-scoped, reversible mapping between original values and surrogate tokens.

Two properties carry the design:

* **Deterministic inside a job** — the same customer gets the same token in every row, so the
  model can correlate and aggregate correctly.
* **Different across jobs** — a fresh salt per job means tokens are not a stable pseudo-identifier
  that accumulates meaning across conversations, and a provider that retains prompts cannot join
  yesterday's to today's.

Originals are encrypted at rest with the application's existing Fernet key. Lookups go through a
keyed HMAC rather than a plain hash, so the fingerprint column is not a rainbow-table target.
"""
from __future__ import annotations

import hashlib
import hmac

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.anonymize.strategies import ENTITY_CODES
from backend.core.config import settings
from backend.core.crypto import decrypt, encrypt
from backend.metadata.models import AnonToken


class Vault:
    def __init__(self, db: Session | None, job_id: str, salt: str, persist: bool = True) -> None:
        # persist=False gives an in-memory vault that neither reads nor writes the token table.
        # The UI preview needs one: it renders a single sample value through a candidate policy
        # and must not leave rows behind, or touch the caller's session at all.
        self.db, self.job_id, self.salt = db, job_id, salt
        self.persist = persist and db is not None
        self._key = hashlib.sha256(f"{settings.secret_key}:{salt}".encode()).digest()
        self._by_fp: dict[str, str] = {}      # fingerprint -> token
        self._by_token: dict[str, str] = {}   # token -> original
        self._counts: dict[str, int] = {}
        self._loaded = False

    # ------------------------------------------------------------------ internals
    def _fingerprint(self, value: str) -> str:
        return hmac.new(self._key, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def _load(self) -> None:
        """Rehydrate an existing job (a follow-up question in the same conversation)."""
        if self._loaded:
            return
        self._loaded = True
        if not self.persist:
            return
        for row in self.db.scalars(select(AnonToken).where(AnonToken.job_id == self.job_id)).all():
            self._by_fp[row.fingerprint] = row.token
            self._by_token[row.token] = decrypt(row.original_enc)
            self._counts[row.entity] = max(self._counts.get(row.entity, 0), row.sequence)

    # ------------------------------------------------------------------ api
    def tokenize(self, value: str, entity: str = "other") -> str:
        self._load()
        fp = self._fingerprint(value)
        if fp in self._by_fp:
            return self._by_fp[fp]
        seq = self._counts.get(entity, 0) + 1
        self._counts[entity] = seq
        token = f"[[{ENTITY_CODES.get(entity, 'OTH')}_{seq:04d}]]"
        if self.persist:
            self.db.add(AnonToken(job_id=self.job_id, token=token, entity=entity, sequence=seq,
                                  fingerprint=fp, original_enc=encrypt(value)))
        self._by_fp[fp] = token
        self._by_token[token] = value
        return token

    def resolve(self, code: str, number: str) -> str | None:
        self._load()
        return self._by_token.get(f"[[{code.upper()}_{int(number):04d}]]")

    def originals(self) -> list[str]:
        self._load()
        return list(self._by_token.values())

    def pairs(self) -> list[tuple[str, str]]:
        """(original, token), for the free-text dictionary pass."""
        self._load()
        return [(original, token) for token, original in self._by_token.items()]

    def day_offset(self) -> int:
        """Per-job constant date shift of roughly +/- 2 years."""
        digest = hmac.new(self._key, b"date-shift", hashlib.sha256).digest()
        return int.from_bytes(digest[:4], "big") % 1461 - 730

    @property
    def size(self) -> int:
        self._load()
        return len(self._by_token)
