"""Natural-language time period resolution. 'now' is anchored to the latest date present in the
data (passed in as `anchor`) so questions like 'last month' work on historical/synthetic datasets."""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
ABBR = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


def _month_mention(t: str):
    """Full month names anywhere ('may' only with a year / 'in may'); abbreviations only with a year."""
    for name, num in sorted(MONTHS.items(), key=lambda kv: -len(kv[0])):
        pat = rf"\b{name}\b(?:\s+(20\d{{2}}))?" if name != "may" else r"(?:\bin\s+may\b(?:\s+(20\d{2}))?|\bmay\s+(20\d{2})\b)"
        if m := re.search(pat, t):
            year = next((g for g in m.groups() if g), None)
            return num, (int(year) if year else None)
    for name, num in ABBR.items():
        if m := re.search(rf"\b{name}\s+(20\d{{2}})\b", t):
            return num, int(m.group(1))
    return None


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # exclusive
    label: str
    grain: str = "month"  # month|quarter|year|custom

    @property
    def key(self) -> str:
        return self.start.strftime("%Y-%m") if self.grain == "month" else self.label

    def to_dict(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "label": self.label, "grain": self.grain}


def _month(y: int, m: int) -> Period:
    end = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return Period(date(y, m, 1), end, date(y, m, 1).strftime("%B %Y"), "month")


def _quarter(y: int, q: int) -> Period:
    sm = 3 * (q - 1) + 1
    em = sm + 3
    end = date(y + (em > 12), em - 12 if em > 12 else em, 1)
    return Period(date(y, sm, 1), end, f"Q{q} {y}", "quarter")


def _year(y: int) -> Period:
    return Period(date(y, 1, 1), date(y + 1, 1, 1), str(y), "year")


def shift(p: Period, n: int) -> Period:
    """Shift a period by n grains (negative = earlier)."""
    if p.grain == "month":
        m = p.start.month - 1 + n
        return _month(p.start.year + m // 12, m % 12 + 1)
    if p.grain == "quarter":
        q = (p.start.month - 1) // 3 + n
        return _quarter(p.start.year + q // 4, q % 4 + 1)
    if p.grain == "year":
        return _year(p.start.year + n)
    days = (p.end - p.start).days
    from datetime import timedelta

    return Period(p.start + timedelta(days=days * n), p.end + timedelta(days=days * n), f"{p.label} shifted {n}", "custom")


def resolve_period(text: str, anchor: date) -> Period | None:
    t = text.lower()
    # explicit YYYY-MM
    if m := re.search(r"\b(20\d{2})-(0[1-9]|1[0-2])\b", t):
        return _month(int(m.group(1)), int(m.group(2)))
    # quarter: Q3 2026 / Q3 / last quarter / this quarter
    if m := re.search(r"\bq([1-4])\s*(20\d{2})?\b", t):
        y = int(m.group(2)) if m.group(2) else anchor.year
        return _quarter(y, int(m.group(1)))
    # a concrete month beats relative phrases: "August vs last year" is August, compared with last year
    if mm := _month_mention(t):
        num, y = mm
        y = y if y else (anchor.year if num <= anchor.month else anchor.year - 1)
        return _month(y, num)
    if "last quarter" in t or "previous quarter" in t:
        cur = _quarter(anchor.year, (anchor.month - 1) // 3 + 1)
        return shift(cur, -1)
    if "this quarter" in t or "current quarter" in t:
        return _quarter(anchor.year, (anchor.month - 1) // 3 + 1)
    if "last year" in t or "previous year" in t:
        return _year(anchor.year - 1)
    if "this year" in t or "ytd" in t or "year to date" in t:
        return _year(anchor.year)
    if "last month" in t or "previous month" in t:
        return shift(default_period(anchor), -1)
    if "this month" in t or "current month" in t or "latest month" in t:
        return default_period(anchor)
    if m := re.search(r"\b(20\d{2})\b", t):
        return _year(int(m.group(1)))
    return None


def default_period(anchor: date) -> Period:
    """Latest *complete* month: if the anchor sits early in a month, that month is partial."""
    p = _month(anchor.year, anchor.month)
    return shift(p, -1) if anchor.day < 25 else p


def comparison_for(text: str, p: Period) -> Period:
    t = text.lower()
    if "last year" in t or "year over year" in t or "yoy" in t or "same period" in t:
        if p.grain == "month":
            return _month(p.start.year - 1, p.start.month)
        if p.grain == "quarter":
            return _quarter(p.start.year - 1, (p.start.month - 1) // 3 + 1)
        return _year(p.start.year - 1)
    return shift(p, -1)
