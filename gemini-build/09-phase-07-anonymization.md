# Phase 07 — The Anonymisation Layer

**Goal:** real data never reaches an AI provider, and the model's answer still comes back
readable. This is the security boundary of the product. A bug here is not a wrong number, it is
a disclosure.

**Depends on:** Phases 00–01, 06.

> **A working implementation of everything in this phase ships with this guide:**
> `reference/anonymizer.py`. Run `python reference/anonymizer.py` — it prints
> `all anonymiser checks passed`. Start from that file rather than asking the model to invent
> the algorithm. The sections below explain *why* it is built that way, because the reasoning is
> what you need when you extend it.

---

## The shape of the problem

```
  real rows ──► apply column policies ──► scrub free text ──► EGRESS GUARD ──► AI provider
                                                                   │
                                                              refuses to send
                                                              if anything leaked

  AI answer ──► find tokens ──► look up in vault ──► real values ──► user
```

Four things have to be true at once, and they pull against each other:

1. **Nothing identifying leaves.** Obvious, and the easy half.
2. **The data stays analytically useful.** If you replace every number with `XXX`, the model
   cannot do arithmetic and the feature is worthless. Anonymisation must be *selective*.
3. **The mapping is reversible** — but only locally, and only for this request.
4. **The model's output can be mapped back**, including when the model has reformatted the
   tokens.

---

## Build these files

```
udal/anonymize/policy.py      per-column rules + sensible defaults
udal/anonymize/vault.py       job-scoped reversible token store
udal/anonymize/engine.py      apply / scrub / restore
udal/anonymize/guard.py       assert_no_leak
udal/anonymize/detect.py      PII detectors + policy suggestion
udal/migrations/004_anon.sql
udal/api/routers/policies.py
tests/unit/test_anonymize.py      port every assertion from reference/anonymizer.py
tests/unit/test_guard.py
```

---

## Strategies

Per column. The user sets these in the UI; `detect.py` proposes defaults.

| Strategy | Reversible | What the model sees | Use for |
|---|---|---|---|
| `passthrough` | n/a | the real value | non-sensitive data — **numbers you want analysed** |
| `pseudonym` | **yes** | `[[PER_0001]]` | names, emails, account ids, anything referenced in the answer |
| `redact` | no | `[[REDACTED]]` | national id, card numbers — values with no analytical use |
| `mask` | no | `jo****` | when a prefix is genuinely useful |
| `hash` | no | `a3f9c1...` | join keys that must match across tables but never be read |
| `generalize` | no | `90000-100000`, `2026-01` | salary bands, dates to month — keeps magnitude |
| `shift` | no | a date moved by a per-job constant | dates where **intervals** matter |

### The two that make this usable

**`passthrough` on numeric columns is the default and that is deliberate.** Revenue, quantity
and counts are what the model needs to actually answer questions. Protecting the *name* attached
to a number is what matters; protecting the number usually destroys the feature for no gain.

**`shift` preserves intervals.** Every date in a job moves by the same offset, so "signed up 59
days before churning" survives while the absolute dates do not. The reference implementation
tests exactly this:

```python
d0, d1 = date.fromisoformat(safe[0]["signup_date"]), date.fromisoformat(safe[1]["signup_date"])
assert (d1 - d0).days == 59          # interval intact
assert safe[0]["signup_date"] != "2026-01-10"   # absolute date gone
```

---

## The token format

```python
TOKEN_RE = re.compile(r"\[\[\s*([A-Za-z]{3})[_-]?(\d+)\s*\]\]")
```

`[[PER_0001]]` — plain ASCII, no markdown meaning, no JSON escaping problems, and not a string
that occurs in business data. The entity code is kept (`PER`, `EML`, `ORG`, `TEL`, `ADR`, `ACC`,
`UID`) because the model reasons far better about "a person" than about an opaque id.

The *reader* is deliberately lenient — `[[ per-1 ]]` resolves — because models lowercase things,
swap underscores for hyphens and insert spaces. Being strict here means losing the ability to
restore a value the model did return, which is the worst of both worlds.

---

## The vault, and why it is scoped per job

```sql
CREATE TABLE anon_token (
    job_id   TEXT NOT NULL,
    token    TEXT NOT NULL,
    entity   TEXT NOT NULL,
    fp       TEXT NOT NULL,    -- HMAC(secret, original). Never the original.
    original TEXT NOT NULL,    -- ENCRYPT AT REST with the Phase 02 Fernet key
    PRIMARY KEY (job_id, token)
);
CREATE UNIQUE INDEX anon_token_fp ON anon_token(job_id, fp);
```

Two properties, both load-bearing:

- **Deterministic within a job.** The same customer gets the same token in every row, so the
  model can see that rows 1 and 3 are one person and aggregate correctly.
- **Different across jobs.** A fresh salt per job means tokens are not a stable pseudo-identifier
  that accumulates meaning across conversations, and a provider that logs prompts cannot join
  yesterday's to today's.

Look-ups go through a **keyed HMAC**, not a plain hash — otherwise the fingerprint column is a
rainbow-table attack against a list of common names.

In the real app, encrypt `original` with the Phase 02 vault. The reference file stores it in
plaintext in an in-memory SQLite database purely so it runs standalone.

---

## Pass 1 — column policies

Straightforward: look up the policy for `(table, column)` and apply it.

**A column with no policy is passed through unchanged.** This fails *open*, which is the wrong
default for security, and it is chosen deliberately: failing closed would redact revenue figures
and make the product useless. The mitigations are that `detect.py` proposes a policy for every
column that looks sensitive, the UI flags unpoliced columns, and **the egress guard is the real
backstop**. Gate test 3 below exists to keep this honest.

---

## Pass 2 — free text, and the trick that makes it work

Free-text columns — support notes, email bodies, descriptions — contain names and addresses
that no column policy covers. Regex detectors catch structured things (email, phone, IBAN) but
no regex knows that "Priya Raman" is a person.

**The dictionary pass solves this.** You already tokenised every customer name in pass 1, so you
already have the list of sensitive values in this job. Use it as a search dictionary over the
free text:

```python
known = sorted(
    ((o, t) for o, t in self._known_pairs() if len(o) >= 3),
    key=lambda pair: len(pair[0]),
    reverse=True,          # longest first: "Acme Corp Ltd" before "Acme Corp"
)
for original, token in known:
    text = re.sub(rf"(?<!\w){re.escape(original)}(?!\w)", token, text, flags=re.IGNORECASE)
```

Longest-first ordering is not optional — replace "Acme Corp" first and "Acme Corp Ltd" becomes
`[[ORG_0001]] Ltd`, which leaks the suffix and corrupts the token.

Then run the regex detectors, which catch values that appear *only* in free text. The reference
test proves both halves:

```python
note = "Called Priya Raman about the renewal; cc priya@acme.com and finance@acme.com."
# -> "Called [[PER_0001]] about the renewal; cc [[EML_0001]] and [[EML_0003]]."
```

`finance@acme.com` appears in no column anywhere — the detector caught it.

---

## The egress guard

```python
def assert_no_leak(payload: str, vault: Vault, min_len: int = 3) -> None:
    leaks = [
        value for value in vault.originals()
        if len(value) >= min_len
        and re.search(rf"(?<!\w){re.escape(value)}(?!\w)", payload, re.IGNORECASE)
    ]
    if leaks:
        raise LeakError(f"{len(leaks)} original value(s) present in outbound payload: {leaks[:3]}")
```

Four rules about how this is used, and they are the whole reason it works:

1. **It runs on the fully serialised request body** — the exact bytes about to go over the wire.
   Not the row list, not the prompt fragment. That way it also covers the system prompt, few-shot
   examples, retry payloads and any field a future refactor forgets to anonymise.
2. **It runs immediately before the HTTP call**, inside the AI client, not in the caller. A new
   call site cannot forget it.
3. **`LeakError` is never caught broadly, never retried, never downgraded to a warning.** The
   request dies and the incident is audited.
4. **There is no bypass.** No debug flag, no "internal" caller, no env var.

### Its known ceiling, stated honestly

Values shorter than `min_len=3` are not checked, because two-character values collide with
ordinary English words and would make the guard fire on everything. Initials and short codes are
therefore not covered by the guard — they are covered by pass 1 only. This is a deliberate
trade-off; document it, and allow an explicit allowlist for a short value that genuinely matters.

---

## Reversal

```python
def deanonymize(text: str, vault: Vault) -> tuple[str, list[str]]:
```

Returns the restored text **and the tokens it could not resolve**. Unresolved tokens are
returned rather than silently left in place, because a model that emits `[[PER_0099]]` when only
`PER_0001` and `PER_0002` exist has invented a person. That is a hallucination, the caller must
be able to detect it, and the UI should flag the answer rather than printing a dangling token.

---

## `detect.py` — proposing policies

Scan a sample of each column and suggest a strategy:

| Signal | Suggested |
|---|---|
| name matches `email`/`mail` or values match the email pattern | `pseudonym` / `email` |
| name matches `phone`/`mobile`/`tel` | `pseudonym` / `phone` |
| `ssn`, `aadhaar`, `pan`, `nin`, `passport`, `card`, `cvv`, `iban` | `redact` |
| `name`, `first_name`, `customer`, `contact`, `employee` | `pseudonym` / `person` |
| `address`, `street`, `postcode`, `zip` | `pseudonym` / `address` |
| `salary`, `compensation`, `income` | `generalize` |
| type is `date`/`datetime` | `shift` |
| numeric, no sensitive name | **`passthrough`** |
| high-cardinality free text | `passthrough`, flagged "scan at send time" |

Suggestions are never auto-applied. The UI shows them pre-selected with a "review before
enabling" banner, and the policy records `accepted_by`.

---

## Validation Gate 07

This is the most important gate in the guide. Everything here is already asserted in
`reference/anonymizer.py` — port each one into `tests/unit/test_anonymize.py`.

**1. The reference implementation passes**

```bash
python reference/anonymizer.py
```

```
all anonymiser checks passed
```

**2. Round trip**

Anonymise rows, feed the tokens through a fake "AI" that echoes them back in a sentence,
de-anonymise, assert the original values are restored exactly.

**3. Selective anonymisation — the per-column requirement**

Assert in one test that, for the same row: a `pseudonym` column is tokenised, a `passthrough`
column is **byte-identical to the original**, and an unpoliced numeric column is still a number
usable for arithmetic.

```python
assert safe[0]["region"] == "West"      # passthrough untouched
assert safe[0]["revenue"] == 500        # still a number, not a string, not a token
```

**4. Determinism within a job, isolation across jobs**

```python
assert safe[0]["name"] == safe[2]["name"]     # same person, same token
assert safe[0]["name"] != safe[1]["name"]     # different people, different tokens
# and a second job assigns different tokens to the same person
```

**5. The guard catches a leak — the negative test**

```python
assert_no_leak(clean_payload, vault)                      # passes
with pytest.raises(LeakError):
    assert_no_leak(clean_payload + " spoke to John Smith", vault)
```

Assert the exception names the leaked value, so an operator can act on it.

**6. Free text is scrubbed by both mechanisms**

One assertion for a name caught by the dictionary pass, one for an email address that exists in
**no** column and is caught only by a detector.

**7. Mangled tokens still resolve**

```python
deanonymize("[[ per_1 ]] and [[PER-0002]] are the top accounts.", vault)
```

Both resolve. Models do this constantly.

**8. Hallucinated tokens are reported**

```python
restored, unresolved = deanonymize("Also [[PER_0099]] churned.", vault)
assert unresolved == ["[[PER_0099]]"]
```

**9. Intervals survive `shift`, magnitude survives `generalize`**

As shown above.

**10. The vault is encrypted at rest**

```bash
python -c "
import sqlite3; c=sqlite3.connect('data/udal.db')
rows=[r[0] for r in c.execute('SELECT original FROM anon_token')]
assert all('Priya' not in r for r in rows), 'PLAINTEXT ORIGINAL IN VAULT'
print('ok')
"
```

**11. Fuzz the guard**

Generate 500 random rows with names containing quotes, accents, emoji, regex metacharacters
(`a.b*c`), and very long values. Anonymise, serialise, assert the guard passes and no original
survives. `re.escape` on every original is what makes this hold — the test is there to prove it
was not forgotten.

---

## Common failures in this phase

| Symptom | Cause |
|---|---|
| The model cannot do arithmetic any more | numeric columns pseudonymised; they should be `passthrough` |
| `[[ORG_0001]] Ltd` in the output | dictionary pass not sorted longest-first |
| The model confuses two customers | tokens not deterministic within the job |
| Tokens in the final answer | `deanonymize` not applied, or unresolved tokens ignored |
| Guard never fires, even on a real leak | checking the row list instead of the serialised body |
| Guard fires on everything | `min_len` too low, or substring matching without word boundaries |
| Crash on a name like `O'Brien (Jr.)` | missing `re.escape` |
| Same token for the same person across sessions | salt not per job |
