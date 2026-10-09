"""The AI privacy boundary.

Real values never reach an AI provider. Columns carrying a policy are replaced with surrogate
tokens, free text is scrubbed, the outbound payload is checked one last time by the egress
guard, and whatever the model returns is mapped back to the real values locally.

This is deliberately separate from `backend.security.access`, which masks PII for *viewers*
based on their role. That protects a human looking at a screen; this protects data from
leaving the machine. An admin sees unmasked rows in the UI and the model still only sees
tokens.
"""
from backend.anonymize.engine import Anonymizer, new_job
from backend.anonymize.guard import LeakError, assert_no_leak
from backend.anonymize.policy import Policy, default_strategy_for, policies_for_tables
from backend.anonymize.strategies import ENTITY_CODES, STRATEGIES, TOKEN_RE
from backend.anonymize.vault import Vault

__all__ = [
    "Anonymizer", "new_job", "LeakError", "assert_no_leak", "Policy",
    "default_strategy_for", "policies_for_tables", "Vault",
    "ENTITY_CODES", "STRATEGIES", "TOKEN_RE",
]
