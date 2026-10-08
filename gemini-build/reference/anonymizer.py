"""Reference implementation of the anonymisation layer (Phase 07).

This is the security boundary of the whole product: original data must never reach the
AI provider, and whatever the AI returns must be mapped back to originals before a human
sees it. The file is deliberately dependency-light and self-checking so you can run it on
its own before wiring it into the app:

    python anonymizer.py

Copy the algorithm, not necessarily the storage. In the real app the vault lives in SQLite
and `original` is encrypted at rest with the Fernet key from Phase 02.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable

# --------------------------------------------------------------------------- token format
# [[PER_0001]] survives a round trip through every major LLM: plain ASCII, no markdown
# meaning, no JSON escaping, and no word that occurs in business data. The reader is
# deliberately lenient because models lowercase things and insert spaces.
TOKEN_RE = re.compile(r"\[\[\s*([A-Za-z]{3})[_-]?(\d+)\s*\]\]")

ENTITY_CODES = {
    "person": "PER", "email": "EML", "phone": "TEL", "org": "ORG",
    "address": "ADR", "account": "ACC", "id": "UID", "other": "OTH",
}

# --------------------------------------------------------------------------- free-text detectors
# These catch PII inside free-text columns that no column policy covers. They run *in
# addition to* the dictionary pass, which is the stronger of the two.
DETECTORS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")),
    ("phone", re.compile(r"(?<!\w)(?:\+\d{1,3}[\s-]?)?(?:\(\d{2,4}\)[\s-]?)?\d{3,5}[\s-]?\d{4,6}(?!\w)")),
    ("account", re.compile(r"\b(?:[A-Z]{2}\d{2}[A-Z0-9]{10,30}|\d{12,19})\b")),
]

STRATEGIES = {"passthrough", "pseudonym", "mask", "redact", "generalize", "shift", "hash"}


@dataclass(frozen=True)
class Policy:
    """What to do with one column. `entity` only matters for pseudonym tokens."""
    table: str
    column: str
    strategy: str = "pseudonym"
    entity: str = "other"
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy {self.strategy!r}, expected one of {sorted(STRATEGIES)}")


class Vault:
    """Job-scoped, reversible mapping between originals and surrogates.

    Scoping by job matters: the same customer gets the same token inside one request (so the
    model can correlate rows) but a different token in the next (so tokens are not a stable
    identifier that leaks across conversations).
    """

    def __init__(self, conn: sqlite3.Connection, job_id: str, secret: bytes) -> None:
        self.conn, self.job_id, self.secret = conn, job_id, secret
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS anon_token (
                job_id   TEXT NOT NULL,
                token    TEXT NOT NULL,
                entity   TEXT NOT NULL,
                fp       TEXT NOT NULL,   -- HMAC of the original; never the original itself
                original TEXT NOT NULL,   -- encrypt this at rest in the real app
                PRIMARY KEY (job_id, token)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS anon_token_fp ON anon_token(job_id, fp);
            """
        )

    def _fingerprint(self, value: str) -> str:
        # Keyed, so the table cannot be attacked with a rainbow table of common names.
        return hmac.new(self.secret, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def tokenize(self, value: str, entity: str) -> str:
        """Stable within the job: the same value always returns the same token."""
        fp = self._fingerprint(value)
        row = self.conn.execute(
            "SELECT token FROM anon_token WHERE job_id = ? AND fp = ?", (self.job_id, fp)
        ).fetchone()
        if row:
            return row[0]
        code = ENTITY_CODES.get(entity, "OTH")
        n = self.conn.execute(
            "SELECT COUNT(*) FROM anon_token WHERE job_id = ? AND entity = ?", (self.job_id, entity)
        ).fetchone()[0]
        token = f"[[{code}_{n + 1:04d}]]"
        self.conn.execute(
            "INSERT INTO anon_token (job_id, token, entity, fp, original) VALUES (?, ?, ?, ?, ?)",
            (self.job_id, token, entity, fp, value),
        )
        return token

    def resolve(self, code: str, number: str) -> str | None:
        token = f"[[{code.upper()}_{int(number):04d}]]"
        row = self.conn.execute(
            "SELECT original FROM anon_token WHERE job_id = ? AND token = ?", (self.job_id, token)
        ).fetchone()
        return row[0] if row else None

    def originals(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT original FROM anon_token WHERE job_id = ?", (self.job_id,))]

    def day_offset(self) -> int:
        """Per-job constant date shift. Intervals between dates survive; absolute dates do not."""
        digest = hmac.new(self.secret, b"date-shift", hashlib.sha256).digest()
        return int.from_bytes(digest[:4], "big") % 1461 - 730  # +/- 2 years


class Anonymizer:
    def __init__(self, vault: Vault, policies: Iterable[Policy]) -> None:
        self.vault = vault
        self.policies = {(p.table.lower(), p.column.lower()): p for p in policies}

    # ---------------------------------------------------------------- per-value strategies
    def _apply(self, policy: Policy, value: Any) -> Any:
        if value is None or value == "":
            return value
        s = str(value)
        match policy.strategy:
            case "passthrough":
                return value
            case "redact":
                return "[[REDACTED]]"
            case "hash":
                return self.vault._fingerprint(s)[:16]
            case "mask":
                keep = int(policy.params.get("keep", 2))
                return s[:keep] + "*" * max(len(s) - keep, 0)
            case "pseudonym":
                return self.vault.tokenize(s, policy.entity)
            case "generalize":
                return self._generalize(policy, value)
            case "shift":
                return self._shift(value)
        raise AssertionError(f"unhandled strategy {policy.strategy}")

    def _generalize(self, policy: Policy, value: Any) -> Any:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            bucket = float(policy.params.get("bucket", 1000))
            low = (float(value) // bucket) * bucket
            return f"{low:g}-{low + bucket:g}"
        d = _as_date(value)
        if d is not None:
            return d.strftime(policy.params.get("format", "%Y-%m"))
        return "[[REDACTED]]"

    def _shift(self, value: Any) -> Any:
        d = _as_date(value)
        if d is None:
            return "[[REDACTED]]"
        return (d + timedelta(days=self.vault.day_offset())).isoformat()

    # ---------------------------------------------------------------- rows and free text
    def anonymize_rows(self, table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Pass 1: column policies. Any column with no policy is passed through untouched,
        so a policy set that forgets a column fails open — Phase 07's gate tests for that."""
        out = []
        for row in rows:
            new = {}
            for col, val in row.items():
                policy = self.policies.get((table.lower(), col.lower()))
                new[col] = self._apply(policy, val) if policy else val
            out.append(new)
        return out

    def scrub_text(self, text: str) -> str:
        """Pass 2: free text. The dictionary pass runs first and is the reason this works:
        every value already tokenised from a structured column becomes a search term, so a
        customer name buried in a support note is caught even though no detector knows names.
        """
        if not text:
            return text
        known = sorted(
            ((o, t) for o, t in self._known_pairs() if len(o) >= 3),
            key=lambda pair: len(pair[0]),
            reverse=True,  # longest first, so "Acme Corp Ltd" wins over "Acme Corp"
        )
        for original, token in known:
            text = re.sub(rf"(?<!\w){re.escape(original)}(?!\w)", token, text, flags=re.IGNORECASE)
        for entity, pattern in DETECTORS:
            text = pattern.sub(lambda m: self.vault.tokenize(m.group(0), entity), text)
        return text

    def _known_pairs(self) -> list[tuple[str, str]]:
        return list(self.vault.conn.execute(
            "SELECT original, token FROM anon_token WHERE job_id = ?", (self.vault.job_id,)))


# --------------------------------------------------------------------------- egress guard
class LeakError(RuntimeError):
    """Raised instead of sending a payload that still contains an original value."""


def assert_no_leak(payload: str, vault: Vault, min_len: int = 3) -> None:
    """The hard boundary. Call this on the fully serialised request body immediately before
    it goes to the AI provider — not earlier, so it also covers prompt text, system messages
    and any field a later refactor forgets to route through the anonymiser.

    ponytail: values shorter than `min_len` are not checked, because single characters
    collide with ordinary words and would make this unusable. Treat very short sensitive
    values (initials, 2-letter codes) as a known ceiling; widen with an allowlist if needed.
    """
    leaks = [
        value for value in vault.originals()
        if len(value) >= min_len and re.search(rf"(?<!\w){re.escape(value)}(?!\w)", payload, re.IGNORECASE)
    ]
    if leaks:
        raise LeakError(f"{len(leaks)} original value(s) present in outbound payload: {leaks[:3]}")


# --------------------------------------------------------------------------- de-anonymise
def deanonymize(text: str, vault: Vault) -> tuple[str, list[str]]:
    """Map the model's answer back to real values.

    Returns the restored text and any tokens that could not be resolved. Unresolved tokens
    are reported rather than silently left in place: a model that invents [[PER_0099]] is
    hallucinating a person, and the caller must be able to see that.
    """
    unresolved: list[str] = []

    def replace(match: re.Match[str]) -> str:
        original = vault.resolve(match.group(1), match.group(2))
        if original is None:
            unresolved.append(match.group(0))
            return match.group(0)
        return original

    return TOKEN_RE.sub(replace, text), unresolved


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


# --------------------------------------------------------------------------- self-check
def demo() -> None:
    conn = sqlite3.connect(":memory:")
    vault = Vault(conn, job_id="job-1", secret=b"unit-test-secret")
    policies = [
        Policy("customers", "name", "pseudonym", entity="person"),
        Policy("customers", "email", "pseudonym", entity="email"),
        Policy("customers", "signup_date", "shift"),
        Policy("customers", "salary", "generalize", params={"bucket": 10_000}),
        Policy("customers", "ssn", "redact"),
        Policy("customers", "region", "passthrough"),
    ]
    anon = Anonymizer(vault, policies)

    rows = [
        {"name": "Priya Raman", "email": "priya@acme.com", "signup_date": "2026-01-10",
         "salary": 94_000, "ssn": "123-45-6789", "region": "West", "revenue": 500},
        {"name": "John Smith", "email": "john@acme.com", "signup_date": "2026-03-10",
         "salary": 51_000, "ssn": "987-65-4321", "region": "East", "revenue": 300},
        {"name": "Priya Raman", "email": "priya@acme.com", "signup_date": "2026-01-10",
         "salary": 94_000, "ssn": "123-45-6789", "region": "West", "revenue": 200},
    ]
    safe = anon.anonymize_rows("customers", rows)

    # 1. sensitive columns are replaced, non-sensitive ones survive untouched
    assert safe[0]["name"].startswith("[[PER_"), safe[0]
    assert safe[0]["ssn"] == "[[REDACTED]]"
    assert safe[0]["region"] == "West", "passthrough column must be unchanged"
    assert safe[0]["revenue"] == 500, "unpoliced numeric column must stay usable for maths"

    # 2. deterministic inside the job: the model can still see rows 1 and 3 are one person
    assert safe[0]["name"] == safe[2]["name"]
    assert safe[0]["name"] != safe[1]["name"]

    # 3. generalisation keeps the magnitude, loses the value
    assert safe[0]["salary"] == "90000-100000", safe[0]["salary"]

    # 4. date shift preserves the interval between dates
    d0, d1 = date.fromisoformat(safe[0]["signup_date"]), date.fromisoformat(safe[1]["signup_date"])
    assert (d1 - d0).days == 59, (d0, d1)
    assert safe[0]["signup_date"] != "2026-01-10"

    # 5. free text: the dictionary pass catches a name no detector would know
    note = "Called Priya Raman about the renewal; cc priya@acme.com and finance@acme.com."
    scrubbed = anon.scrub_text(note)
    assert "Priya Raman" not in scrubbed and "priya@acme.com" not in scrubbed, scrubbed
    assert "finance@acme.com" not in scrubbed, "detector must catch an address not in any column"
    assert scrubbed.count(safe[0]["name"]) == 1

    # 6. the egress guard refuses a payload that still contains an original
    payload = "\n".join(f"{r['name']} {r['region']}" for r in safe) + "\n" + scrubbed
    assert_no_leak(payload, vault)  # clean payload passes
    try:
        assert_no_leak(payload + " ...spoke to John Smith again", vault)
        raise AssertionError("guard failed to catch a leaked original")
    except LeakError as exc:
        assert "John Smith" in str(exc)

    # 7. round trip, including a model that lowercased and spaced the token
    answer = f"{safe[0]['name']} and {safe[1]['name'].lower().replace('[[', '[[ ')} are the top accounts."
    restored, unresolved = deanonymize(answer, vault)
    assert "Priya Raman" in restored and "John Smith" in restored, restored
    assert unresolved == []

    # 8. a token the model invented is surfaced, not silently dropped
    restored, unresolved = deanonymize("Also [[PER_0099]] churned.", vault)
    assert unresolved == ["[[PER_0099]]"] and "[[PER_0099]]" in restored

    # 9. tokens do not carry across jobs
    other = Vault(conn, job_id="job-2", secret=b"unit-test-secret")
    assert other.tokenize("Priya Raman", "person") == "[[PER_0001]]"
    assert other.resolve("PER", "1") == "Priya Raman"
    assert Vault(conn, "job-1", b"unit-test-secret").resolve("PER", "1") == "Priya Raman"

    print("all anonymiser checks passed")
    print(f"  sample row out : {safe[0]}")
    print(f"  scrubbed note  : {scrubbed}")
    print(f"  restored answer: {deanonymize(answer, vault)[0]}")


if __name__ == "__main__":
    demo()
