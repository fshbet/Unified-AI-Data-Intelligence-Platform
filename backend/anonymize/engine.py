"""Apply policies to tabular results and free text; map the model's answer back.

Two passes, in this order:

1. **Column policies.** Each value is replaced according to its column's strategy.
2. **Free text.** The values tokenised in pass 1 become a search dictionary over any text
   column, which is what catches a customer's name buried in a support note — no regex knows
   that "Priya Raman" is a person, but we already know it from the structured columns. Regex
   detectors then catch what appears *only* in free text.
"""
from __future__ import annotations

import re
import secrets
from typing import Any

from sqlalchemy.orm import Session

from backend.anonymize.policy import Policy
from backend.anonymize.strategies import (
    DETECTORS,
    REDACTED,
    TOKEN_RE,
    generalize,
    mask,
    shift,
)
from backend.anonymize.vault import Vault
from backend.metadata.models import AnonJob

# Most protective first. Used when a result column cannot be traced to one table and several
# tables define the same column name — we take the strongest policy rather than the first.
STRENGTH = {"redact": 6, "hash": 5, "mask": 4, "pseudonym": 3, "generalize": 2, "shift": 1, "passthrough": 0}

# Values below this length are never scanned for in free text: substring hits on "Li" or "An"
# would corrupt ordinary prose. Matches the egress guard's floor.
MIN_DICT_LEN = 3


def new_job(db: Session, conversation_id: str | None = None, user_id: str | None = None) -> AnonJob:
    job = AnonJob(conversation_id=conversation_id, user_id=user_id, salt=secrets.token_hex(16))
    db.add(job)
    db.flush()
    return job


class Anonymizer:
    def __init__(self, db: Session | None, job: AnonJob, policies: dict[tuple[str, str], Policy],
                 persist: bool = True) -> None:
        self.db, self.job = db, job
        self.vault = Vault(db, job.id, job.salt, persist=persist)
        self.policies = policies
        # Columns that actually had a protective strategy applied during this job, as opposed to
        # columns that merely have a policy defined. This is what the API reports.
        self.protected_columns: set[str] = set()
        self._by_column: dict[str, Policy] = {}
        for (_table, column), p in policies.items():
            cur = self._by_column.get(column)
            if cur is None or STRENGTH[p.strategy] > STRENGTH[cur.strategy]:
                self._by_column[column] = p

    # ------------------------------------------------------------------ lookup
    def _policy(self, column: str, table: str | None) -> Policy | None:
        col = (column or "").lower()
        if table:
            p = self.policies.get((table.lower(), col))
            if p is not None:
                return p
        # A tool result from a join or an ad-hoc SELECT cannot always name its table. Falling
        # back to the column name across every table errs toward protecting too much, which is
        # the right direction for a privacy control.
        return self._by_column.get(col)

    # ------------------------------------------------------------------ values
    def _apply(self, policy: Policy, value: Any) -> Any:
        if value is None or value == "":
            return value
        match policy.strategy:
            case "passthrough":
                return value
            case "redact":
                return REDACTED
            case "hash":
                return self.vault._fingerprint(str(value))[:16]
            case "mask":
                return mask(value, policy.params)
            case "pseudonym":
                return self.vault.tokenize(str(value), policy.entity)
            case "generalize":
                return generalize(value, policy.params)
            case "shift":
                return shift(value, self.vault.day_offset())
        raise AssertionError(f"unhandled strategy {policy.strategy}")

    # ------------------------------------------------------------------ results
    def anonymize_result(
        self, columns: list[str], rows: list[list[Any]], table: str | None = None
    ) -> tuple[list[list[Any]], int]:
        """Returns (rows, number of columns actually protected)."""
        policies = [self._policy(c, table) for c in columns]
        protected = sum(1 for p in policies if p and p.strategy != "passthrough")
        if not protected:
            # Still scrub free text: an unpoliced note column can hold a name from elsewhere.
            return [[self._maybe_text(v) for v in row] for row in rows], 0
        out = []
        for row in rows:
            new = []
            for value, policy in zip(row, policies):
                if policy and policy.strategy != "passthrough":
                    self.protected_columns.add(f"{policy.table}.{policy.column}")
                    new.append(self._apply(policy, value))
                else:
                    new.append(self._maybe_text(value))
            out.append(new)
        return out, protected

    def _maybe_text(self, value: Any) -> Any:
        """Scrub anything long enough to be prose rather than a code or a label."""
        if isinstance(value, str) and len(value) >= 12 and " " in value:
            return self.scrub_text(value)
        return value

    def anonymize_mapping(self, data: Any, table: str | None = None) -> Any:
        """Recursively anonymise a dict/list tool payload (schema samples, profiles, charts)."""
        if isinstance(data, dict):
            out = {}
            for key, value in data.items():
                policy = self._policy(key, table)
                if policy and policy.strategy != "passthrough" and not isinstance(value, (dict, list)):
                    self.protected_columns.add(f"{policy.table}.{policy.column}")
                    out[key] = self._apply(policy, value)
                elif key in {"samples", "sample_values", "top_values", "values", "frequent"}:
                    out[key] = self._anon_samples(value, key, table, data)
                else:
                    out[key] = self.anonymize_mapping(value, table)
            return out
        if isinstance(data, list):
            return [self.anonymize_mapping(v, table) for v in data]
        return self._maybe_text(data)

    def _anon_samples(self, value: Any, key: str, table: str | None, parent: dict) -> Any:
        """Sample values are attached to a column, so the policy comes from the sibling `name`."""
        policy = self._policy(str(parent.get("name") or parent.get("column") or key), table)
        if not policy or policy.strategy == "passthrough":
            return self.anonymize_mapping(value, table)
        if isinstance(value, list):
            return [self._apply(policy, v) for v in value]
        if isinstance(value, dict):
            return {str(self._apply(policy, k)): v for k, v in value.items()}
        return self._apply(policy, value)

    # ------------------------------------------------------------------ free text
    def scrub_text(self, text: str) -> str:
        if not text:
            return text
        pairs = sorted(
            ((o, t) for o, t in self.vault.pairs() if len(o) >= MIN_DICT_LEN),
            key=lambda pair: len(pair[0]),
            reverse=True,  # longest first, or "Acme Corp" eats "Acme Corp Ltd" and leaks " Ltd"
        )
        for original, token in pairs:
            text = re.sub(rf"(?<!\w){re.escape(original)}(?!\w)", token, text, flags=re.IGNORECASE)
        for entity, pattern in DETECTORS:
            text = pattern.sub(lambda m: self.vault.tokenize(m.group(0), entity), text)
        return text

    # ------------------------------------------------------------------ reversal
    def deanonymize(self, text: str) -> tuple[str, list[str]]:
        """Restore real values. Returns (text, tokens that could not be resolved).

        Unresolved tokens are reported rather than silently left in place: a model emitting
        [[PER_0099]] when only two people exist has invented one, and the caller has to be able
        to see that rather than print a dangling token at a user.
        """
        if not text:
            return text, []
        unresolved: list[str] = []

        def replace(match: re.Match[str]) -> str:
            original = self.vault.resolve(match.group(1), match.group(2))
            if original is None:
                unresolved.append(match.group(0))
                return match.group(0)
            return original

        return TOKEN_RE.sub(replace, text), unresolved

    @property
    def tokens_issued(self) -> int:
        return self.vault.size
