"""The AI privacy boundary.

These are the tests that decide whether the product is safe to point at real data, so they
assert the *negative* cases as hard as the positive ones: the guard must refuse, the vault must
not leak across jobs, and a hallucinated token must be reported rather than quietly printed.
"""
from __future__ import annotations

from datetime import date

import pytest

from backend.anonymize import Anonymizer, LeakError, Policy, assert_no_leak, new_job
from backend.anonymize.policy import default_strategy_for
from backend.metadata.models import AnonToken, Column


def _policies() -> dict[tuple[str, str], Policy]:
    ps = [
        Policy("customers", "name", "pseudonym", "person"),
        Policy("customers", "email", "pseudonym", "email"),
        Policy("customers", "signup_date", "shift"),
        Policy("customers", "salary", "generalize", params={"bucket": 10_000}),
        Policy("customers", "ssn", "redact"),
        Policy("customers", "region", "passthrough"),
        Policy("customers", "note", "passthrough"),
    ]
    return {(p.table, p.column): p for p in ps}


@pytest.fixture
def anon(db):
    return Anonymizer(db, new_job(db, conversation_id="c1"), _policies())


COLUMNS = ["name", "email", "signup_date", "salary", "ssn", "region", "revenue"]
ROWS = [
    ["Priya Raman", "priya@acme.com", "2026-01-10", 94_000, "123-45-6789", "West", 500],
    ["John Smith", "john@acme.com", "2026-03-10", 51_000, "987-65-4321", "East", 300],
    ["Priya Raman", "priya@acme.com", "2026-01-10", 94_000, "123-45-6789", "West", 200],
]


def test_sensitive_columns_replaced_and_safe_columns_untouched(anon):
    rows, protected = anon.anonymize_result(COLUMNS, [r[:] for r in ROWS], "customers")
    assert protected == 5
    assert rows[0][0].startswith("[[PER_")
    assert rows[0][1].startswith("[[EML_")
    assert rows[0][4] == "[[REDACTED]]"
    # The whole point of selective anonymisation: analysis must still be possible.
    assert rows[0][5] == "West", "passthrough column must be byte-identical"
    assert rows[0][6] == 500 and isinstance(rows[0][6], int), "unpoliced number must stay a number"


def test_tokens_are_deterministic_within_a_job(anon):
    rows, _ = anon.anonymize_result(COLUMNS, [r[:] for r in ROWS], "customers")
    assert rows[0][0] == rows[2][0], "same person must get the same token so the model can aggregate"
    assert rows[0][0] != rows[1][0]


def test_tokens_do_not_carry_across_jobs(db):
    a = Anonymizer(db, new_job(db), _policies())
    b = Anonymizer(db, new_job(db), _policies())
    tok_a = a.vault.tokenize("Priya Raman", "person")
    tok_b = b.vault.tokenize("Priya Raman", "person")
    db.flush()
    assert a.vault.resolve("PER", "1") == "Priya Raman"
    assert b.vault.resolve("PER", "1") == "Priya Raman"
    # Same ordinal, but the two jobs are independent: neither can resolve the other's tokens.
    assert tok_a == tok_b == "[[PER_0001]]"
    assert a.job.id != b.job.id and a.job.salt != b.job.salt


def test_generalize_keeps_magnitude_and_shift_keeps_intervals(anon):
    rows, _ = anon.anonymize_result(COLUMNS, [r[:] for r in ROWS], "customers")
    assert rows[0][3] == "90000-100000"
    d0, d1 = date.fromisoformat(rows[0][2]), date.fromisoformat(rows[1][2])
    assert (d1 - d0).days == 59, "the interval between dates must survive the shift"
    assert rows[0][2] != "2026-01-10", "the absolute date must not"


def test_free_text_is_scrubbed_by_dictionary_and_detector(anon):
    anon.anonymize_result(COLUMNS, [r[:] for r in ROWS], "customers")
    note = "Called Priya Raman about the renewal; cc priya@acme.com and finance@acme.com."
    out = anon.scrub_text(note)
    assert "Priya Raman" not in out, "dictionary pass must catch a name no regex knows"
    assert "priya@acme.com" not in out
    assert "finance@acme.com" not in out, "detector must catch an address in no column at all"


def test_longest_match_wins_so_a_suffix_cannot_leak(db):
    anon = Anonymizer(db, new_job(db), {("t", "org"): Policy("t", "org", "pseudonym", "org")})
    anon.anonymize_result(["org"], [["Acme Corp Ltd"], ["Acme Corp"]], "t")
    out = anon.scrub_text("Invoice from Acme Corp Ltd is overdue.")
    assert "Acme" not in out and "Ltd" not in out, out


def test_guard_passes_clean_payload_and_refuses_a_leak(anon):
    rows, _ = anon.anonymize_result(COLUMNS, [r[:] for r in ROWS], "customers")
    payload = "\n".join(str(r) for r in rows)
    assert_no_leak(payload, anon.vault)

    with pytest.raises(LeakError) as exc:
        assert_no_leak(payload + " ...spoke to John Smith again", anon.vault)
    assert "John Smith" in str(exc.value), "the operator cannot act on an unnamed leak"


def test_guard_survives_regex_metacharacters_and_unicode(db):
    anon = Anonymizer(db, new_job(db), {("t", "name"): Policy("t", "name", "pseudonym", "person")})
    nasty = ["a.b*c+d", "O'Brien (Jr.)", "Zoë Müller", "[bracketed]", "back\\slash", "emoji 🙂 name"]
    rows, _ = anon.anonymize_result(["name"], [[n] for n in nasty], "t")
    payload = str(rows)
    assert_no_leak(payload, anon.vault)
    for original in nasty:
        with pytest.raises(LeakError):
            assert_no_leak(payload + " " + original, anon.vault)


def test_round_trip_restores_originals_including_mangled_tokens(anon):
    rows, _ = anon.anonymize_result(COLUMNS, [r[:] for r in ROWS], "customers")
    a, b = rows[0][0], rows[1][0]
    answer = f"{a} and {b.lower().replace('[[', '[[ ')} are the top accounts."
    restored, unresolved = anon.deanonymize(answer)
    assert "Priya Raman" in restored and "John Smith" in restored
    assert unresolved == [] and "[[" not in restored


def test_hallucinated_token_is_reported_not_dropped(anon):
    anon.anonymize_result(COLUMNS, [r[:] for r in ROWS], "customers")
    restored, unresolved = anon.deanonymize("Also [[PER_0099]] churned.")
    assert unresolved == ["[[PER_0099]]"]
    assert "[[PER_0099]]" in restored, "the token stays visible so the answer can be flagged"


def test_nested_tool_payloads_are_anonymised(anon):
    payload = {
        "table": "customers",
        "columns": [
            {"name": "email", "samples": ["priya@acme.com", "john@acme.com"]},
            {"name": "revenue", "samples": [500, 300]},
        ],
    }
    out = anon.anonymize_mapping(payload, "customers")
    assert all(s.startswith("[[EML_") for s in out["columns"][0]["samples"])
    assert out["columns"][1]["samples"] == [500, 300], "numeric samples stay usable"


def test_unknown_table_falls_back_to_the_strongest_column_policy(anon):
    # An ad-hoc join result cannot always name its table; protecting too much is the safe way
    # to be wrong for a privacy control.
    rows, protected = anon.anonymize_result(["name", "region"], [["Priya Raman", "West"]], None)
    assert protected == 1 and rows[0][0].startswith("[[PER_") and rows[0][1] == "West"


def test_vault_is_encrypted_at_rest(db, anon):
    anon.vault.tokenize("Priya Raman", "person")
    db.flush()
    stored = [t.original_enc for t in db.query(AnonToken).all()]
    assert stored and all("Priya" not in s for s in stored), "plaintext original in the vault"
    assert all("Priya" not in t.fingerprint for t in db.query(AnonToken).all())


@pytest.mark.parametrize(
    "pii_type,sensitivity,expected",
    [
        ("email", "pii", "pseudonym"),
        ("name", "pii", "pseudonym"),
        ("aadhaar", "restricted", "redact"),
        ("credit_card", "restricted", "redact"),
        ("salary", "sensitive", "generalize"),
        ("date_of_birth", "pii", "shift"),
        (None, "public", "passthrough"),
        (None, "restricted", "redact"),
    ],
)
def test_defaults_auto_protect_detected_pii(pii_type, sensitivity, expected):
    col = Column(column_name="x", data_type="text", pii_type=pii_type, sensitivity=sensitivity)
    assert default_strategy_for(col)[0] == expected


def test_numeric_columns_are_not_protected_by_default():
    """Regression: tokenising numbers breaks every metric the investigation engine computes."""
    col = Column(column_name="revenue", data_type="numeric", logical_type="number", sensitivity="public")
    assert default_strategy_for(col)[0] == "passthrough"


@pytest.mark.parametrize("value,expected", [
    (94_000, "90000-100000"),
    ("94000", "90000-100000"),      # profiled sample values arrive as strings
    ("219,995", "210000-220000"),   # and sometimes formatted
    (94_000.5, "90000-100000"),
])
def test_generalize_bands_numbers_however_they_arrive(db, value, expected):
    """Regression: a numeric string fell through to [[REDACTED]], so the UI preview for a
    salary column disagreed with what the model actually received at query time."""
    anon = Anonymizer(db, new_job(db), {("t", "salary"): Policy("t", "salary", "generalize", params={"bucket": 10_000})})
    rows, _ = anon.anonymize_result(["salary"], [[value]], "t")
    assert rows[0][0] == expected


@pytest.mark.parametrize("value,expected", [
    (2_117_995, "2000000-3000000"),
    (94_000, "90000-100000"),
    (12, "10-20"),
])
def test_auto_bucket_never_narrows_to_the_original_value(db, value, expected):
    """A fixed bucket either pins a large value (2117000-2118000) or flattens a small one."""
    anon = Anonymizer(db, new_job(db), {("t", "amount"): Policy("t", "amount", "generalize")})
    rows, _ = anon.anonymize_result(["amount"], [[value]], "t")
    assert rows[0][0] == expected


def test_protected_column_count_reflects_what_actually_fired(anon):
    """Regression: the API reported 'columns_protected: 0' alongside 17 issued tokens, because
    the count was kept by the toolbox and missed the investigation and free-text paths."""
    assert anon.protected_columns == set()
    anon.anonymize_result(COLUMNS, [r[:] for r in ROWS], "customers")
    assert len(anon.protected_columns) == 5
    assert "customers.region" not in anon.protected_columns, "passthrough must not be counted"
