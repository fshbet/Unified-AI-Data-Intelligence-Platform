"""Token format, per-value strategies and the free-text detectors.

Kept free of database imports so it can be reasoned about (and tested) on its own.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

# [[PER_0001]] survives a round trip through every model we support: plain ASCII, no markdown
# meaning, nothing to escape in JSON, and not a string that occurs in business data.
# The READER is deliberately lenient — models lowercase tokens, swap _ for -, and add spaces.
# Being strict here would mean failing to restore a value the model did return, which is worse
# than accepting a sloppy one.
TOKEN_RE = re.compile(r"\[\[\s*([A-Za-z]{3})[_-]?(\d+)\s*\]\]")
REDACTED = "[[REDACTED]]"

ENTITY_CODES = {
    "person": "PER", "email": "EML", "phone": "TEL", "org": "ORG",
    "address": "ADR", "account": "ACC", "id": "UID", "other": "OTH",
}

STRATEGIES = ("passthrough", "pseudonym", "redact", "mask", "hash", "generalize", "shift")

# Catch PII inside free text that no column policy covers. These run *after* the dictionary
# pass in engine.py, which is the stronger of the two mechanisms.
DETECTORS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")),
    ("phone", re.compile(r"(?<!\w)(?:\+\d{1,3}[\s-]?)?(?:\(\d{2,4}\)[\s-]?)?\d{3,5}[\s-]?\d{4,6}(?!\w)")),
    ("account", re.compile(r"\b(?:[A-Z]{2}\d{2}[A-Z0-9]{10,30}|\d{12,19})\b")),
]


def as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except (ValueError, TypeError):
        return None


def _as_number(value: Any) -> float | None:
    """Numbers arrive as real numerics from SQL but as strings from profiled sample values.
    Both must band identically, or the UI preview would not match what the model actually sees.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _auto_bucket(n: float) -> float:
    """A band roughly 1/10th the magnitude of the value.

    A fixed default would be wrong at both ends: a bucket of 1000 turns a 2,117,995 salary into
    "2117000-2118000", which pins it to within 1000 and anonymises nothing, while turning a
    value of 12 into "0-1000". Scaling keeps the band informative without being identifying.
    """
    magnitude = len(str(int(abs(n)))) if abs(n) >= 1 else 1
    # 10 bands per order of magnitude: 12 -> 10-20, 94000 -> 90000-100000. One digit finer and
    # a two-digit value lands in a band of 1, which is the original value with extra steps.
    return float(10 ** max(magnitude - 1, 1))


def _fmt_num(n: float) -> str:
    # str/%g would render 2117000 as "2.117e+06"; a band a human cannot read is a band nobody trusts.
    return str(int(n)) if n == int(n) else f"{n:.2f}"


def generalize(value: Any, params: dict) -> Any:
    """Keep the magnitude, lose the value. Numbers become a band, dates become a month."""
    if isinstance(value, bool):
        return value
    number = _as_number(value)
    if number is not None:
        bucket = float(params.get("bucket") or _auto_bucket(number))
        low = (number // bucket) * bucket
        return f"{_fmt_num(low)}-{_fmt_num(low + bucket)}"
    d = as_date(value)
    if d is not None:
        return d.strftime(params.get("format", "%Y-%m"))
    return REDACTED


def shift(value: Any, day_offset: int) -> Any:
    """Move a date by a per-job constant so INTERVALS survive while absolute dates do not.

    This is what lets the model still conclude "churned 59 days after signup" from data whose
    real dates it has never seen.
    """
    d = as_date(value)
    if d is None:
        return REDACTED
    return (d + timedelta(days=day_offset)).isoformat()


def mask(value: Any, params: dict) -> str:
    s = str(value)
    keep = int(params.get("keep", 2) or 2)
    return s[:keep] + "*" * max(len(s) - keep, 0)
