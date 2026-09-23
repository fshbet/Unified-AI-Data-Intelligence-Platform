"""PII / sensitive data detection by column name and value patterns."""
from __future__ import annotations

import re

PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$"),
    "phone": re.compile(r"^\+?[\d\s\-()]{8,16}$"),
    "aadhaar": re.compile(r"^\d{4}\s?\d{4}\s?\d{4}$"),
    "pan": re.compile(r"^[A-Z]{5}\d{4}[A-Z]$"),
    "credit_card": re.compile(r"^(?:\d[ -]?){13,19}$"),
    "ip_address": re.compile(r"^(\d{1,3}\.){3}\d{1,3}$"),
    "bank_account": re.compile(r"^\d{9,18}$"),
}
NAME_HINTS: dict[str, tuple[str, ...]] = {
    "email": ("email", "e_mail", "mail"),
    "phone": ("phone", "mobile", "contact_no", "telephone"),
    "name": ("first_name", "last_name", "full_name", "employee_name", "customer_name", "person_name", "contact_name"),
    "address": ("address", "street", "postal", "zipcode", "pincode"),
    "aadhaar": ("aadhaar", "aadhar", "uid_no"),
    "pan": ("pan_no", "pan_number", "pan"),
    "bank_account": ("account_no", "account_number", "iban", "bank_acc"),
    "credit_card": ("card_number", "credit_card", "cc_num"),
    "date_of_birth": ("dob", "birth_date", "date_of_birth"),
    "salary": ("salary", "ctc", "compensation", "pay_rate", "wage"),
    "national_id": ("ssn", "passport", "national_id", "driver_license"),
    "employee_id": ("employee_id", "emp_id", "staff_id", "employee_code"),
}
# sensitivity tier per PII type
TIER = {
    "aadhaar": "restricted", "pan": "restricted", "credit_card": "restricted", "bank_account": "restricted",
    "national_id": "restricted", "email": "pii", "phone": "pii", "name": "pii", "address": "pii",
    "date_of_birth": "pii", "salary": "sensitive", "ip_address": "sensitive", "employee_id": "sensitive",
}


def classify_column(name: str, samples: list[str]) -> dict:
    n = name.lower()
    for pii_type, hints in NAME_HINTS.items():
        if any(h == n or n.endswith("_" + h) or n.startswith(h + "_") or (len(h) > 4 and h in n) for h in hints):
            return {"pii_type": pii_type, "sensitivity": TIER[pii_type], "reason": f"column name matches '{pii_type}'"}
    vals = [s for s in samples if s and s.lower() not in {"none", "nan", "null"}]
    if len(vals) >= 3:
        for pii_type in ("email", "aadhaar", "pan", "credit_card", "ip_address"):
            if sum(bool(PATTERNS[pii_type].match(v.strip())) for v in vals) / len(vals) >= 0.8:
                if pii_type == "credit_card" and not all(_luhn(v) for v in vals):
                    continue
                return {"pii_type": pii_type, "sensitivity": TIER[pii_type], "reason": f"values match {pii_type} pattern"}
    return {"pii_type": None, "sensitivity": "public", "reason": None}


def _luhn(s: str) -> bool:
    digits = [int(c) for c in re.sub(r"\D", "", s)]
    if len(digits) < 13:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def mask_value(value, pii_type: str | None) -> str | None:
    if value is None:
        return None
    s = str(value)
    if pii_type == "email" and "@" in s:
        local, _, dom = s.partition("@")
        return f"{local[:1]}***@{dom}"
    if len(s) <= 4:
        return "****"
    return s[:2] + "*" * (len(s) - 4) + s[-2:]
