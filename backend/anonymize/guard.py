"""The egress guard — the hard boundary.

Call this on the fully serialised request body immediately before it goes to an AI provider.
Not on the row list, and not earlier: checking the serialised body is what makes it cover the
system prompt, the tool-result messages, few-shot examples, retries, and any field a later
refactor forgets to route through the anonymiser.
"""
from __future__ import annotations

import re

from backend.anonymize.vault import Vault

# ponytail: values shorter than this are not checked. Two-character values collide with ordinary
# words ("Li", "Jo", "An") and would make the guard fire on every payload, which in practice means
# someone turns it off. Short sensitive values are covered by the column policy only — a known
# ceiling, documented in docs/security.md. Raise `min_len` or allowlist specific values if needed.
MIN_CHECKED_LEN = 3


class LeakError(RuntimeError):
    """An original value was about to leave the machine. Never caught broadly, never retried."""


def assert_no_leak(payload: str, vault: Vault, min_len: int = MIN_CHECKED_LEN) -> None:
    if not payload:
        return
    leaks = [
        value
        for value in vault.originals()
        if len(value) >= min_len
        and re.search(rf"(?<!\w){re.escape(value)}(?!\w)", payload, re.IGNORECASE)
    ]
    if leaks:
        # The message names what leaked because an operator cannot fix this without knowing.
        # The API handler must NOT pass this text through to an HTTP response.
        raise LeakError(
            f"{len(leaks)} original value(s) present in the outbound AI payload: "
            f"{leaks[:3]}{' ...' if len(leaks) > 3 else ''}"
        )
