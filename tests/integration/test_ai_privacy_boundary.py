"""Proof that real data does not reach the AI provider.

The unit tests in tests/unit/test_anonymize.py prove the engine is correct. These prove the
engine is actually *reached* on the real path — which is the failure mode that matters, and the
one a unit test can never catch. Everything here asserts on the bytes a stub provider really
received, not on an intermediate Python object.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from sqlalchemy import select

from backend.ai.providers import AIProvider, OpenAICompatibleProvider
from backend.anonymize import Anonymizer, LeakError, Policy, new_job
from backend.metadata.models import AIProviderConfig, Conversation, Table, User

CAPTURED: list[dict] = []
REPLY = "Revenue fell in [[ORG_0001]]. The largest account affected was [[PER_0001]]."


class _AIStub(BaseHTTPRequestHandler):
    """Records every request body verbatim, then returns a canned completion."""

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
        CAPTURED.append({"path": self.path, "body": raw})
        body = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": REPLY}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def stub():
    srv = HTTPServer(("127.0.0.1", 0), _AIStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown()


@pytest.fixture(autouse=True)
def _clear():
    CAPTURED.clear()


SECRETS = ["Priya Raman", "priya@acme.com", "123-45-6789"]
COLUMNS = ["name", "email", "ssn", "region", "revenue"]
ROWS = [["Priya Raman", "priya@acme.com", "123-45-6789", "West", 500]]
POLICIES = {
    ("customers", "name"): Policy("customers", "name", "pseudonym", "person"),
    ("customers", "email"): Policy("customers", "email", "pseudonym", "email"),
    ("customers", "ssn"): Policy("customers", "ssn", "redact"),
    ("customers", "region"): Policy("customers", "region", "passthrough"),
}


def _provider(stub: str, vault) -> OpenAICompatibleProvider:
    p = OpenAICompatibleProvider(model="test-model", api_key="k", base_url=stub)
    p.vault, p.require_vault = vault, True
    return p


def test_provider_receives_tokens_and_no_original_value(db, stub):
    """The central assertion of the whole feature."""
    anon = Anonymizer(db, new_job(db), POLICIES)
    rows, protected = anon.anonymize_result(COLUMNS, [r[:] for r in ROWS], "customers")
    assert protected == 3

    provider = _provider(stub, anon.vault)
    provider.chat([
        {"role": "system", "content": "You are an analyst."},
        {"role": "user", "content": anon.scrub_text("why did Priya Raman churn?")},
        {"role": "tool", "tool_call_id": "1", "content": json.dumps({"columns": COLUMNS, "rows": rows})},
    ])

    body = CAPTURED[0]["body"]
    for secret in SECRETS:
        assert secret not in body, f"{secret!r} reached the provider"
    assert "[[PER_" in body and "[[EML_" in body, "the model must still receive usable tokens"
    assert "West" in body, "non-sensitive values must survive, or analysis is impossible"
    assert "500" in body


def test_the_question_itself_is_anonymised(db, stub):
    """Someone will type a real customer name into the question box."""
    anon = Anonymizer(db, new_job(db), POLICIES)
    anon.anonymize_result(COLUMNS, [r[:] for r in ROWS], "customers")
    provider = _provider(stub, anon.vault)
    provider.chat([{"role": "user", "content": anon.scrub_text("why did Priya Raman churn?")}])
    assert "Priya Raman" not in CAPTURED[0]["body"]
    assert "[[PER_0001]]" in CAPTURED[0]["body"]


def test_a_leak_is_blocked_and_nothing_is_sent(db, stub):
    """The guard must fire on the real path, and the request must not happen at all."""
    anon = Anonymizer(db, new_job(db), POLICIES)
    anon.anonymize_result(COLUMNS, [r[:] for r in ROWS], "customers")
    provider = _provider(stub, anon.vault)

    with pytest.raises(LeakError) as exc:
        provider.chat([{"role": "user", "content": "Summarise the account of Priya Raman."}])
    assert "Priya Raman" in str(exc.value)
    assert CAPTURED == [], "the payload was still transmitted despite the guard raising"


def test_every_provider_entry_point_is_guarded(db, stub):
    """chat, stream and embeddings each build their own body; all must pass through _wire."""
    anon = Anonymizer(db, new_job(db), POLICIES)
    anon.anonymize_result(COLUMNS, [r[:] for r in ROWS], "customers")
    provider = _provider(stub, anon.vault)
    leak = [{"role": "user", "content": "about Priya Raman"}]

    with pytest.raises(LeakError):
        provider.chat(leak)
    with pytest.raises(LeakError):
        list(provider.stream(leak))
    provider.embedding_model = "embed"
    with pytest.raises(LeakError):
        provider.generate_embedding(["Priya Raman works in West"])
    assert CAPTURED == []


def test_calling_a_provider_without_a_vault_fails_closed():
    """A new call site that forgets the vault must not silently send plaintext."""
    p = OpenAICompatibleProvider(model="m")
    p.require_vault = True
    with pytest.raises(LeakError, match="without an anonymisation vault"):
        p._wire({"messages": [{"role": "user", "content": "anything"}]})


def test_no_provider_call_site_bypasses_the_guard():
    """Regression: a future provider must not reintroduce a raw `json=body` call."""
    import inspect

    from backend.ai import providers

    source = inspect.getsource(providers)
    assert "json=body" not in source, "a provider call site bypasses _wire()"
    assert source.count("self._wire(") == 5, "a provider call site was added without the guard"


def test_answer_is_restored_before_it_reaches_the_user(db, stub):
    anon = Anonymizer(db, new_job(db), POLICIES)
    anon.anonymize_result(COLUMNS, [r[:] for r in ROWS], "customers")
    anon.vault.tokenize("Acme Corp", "org")

    restored, unresolved = anon.deanonymize(REPLY)
    assert "Priya Raman" in restored and "Acme Corp" in restored
    assert "[[" not in restored and unresolved == []


def test_streamed_tokens_are_never_shown_half_written(db):
    """The model emits '[[PER_' and '0001]]' in separate chunks."""
    from backend.ai.orchestrator import _StreamRestorer

    anon = Anonymizer(db, new_job(db), POLICIES)
    anon.anonymize_result(COLUMNS, [r[:] for r in ROWS], "customers")

    restorer = _StreamRestorer(anon)
    shown = "".join(restorer.feed(c) for c in ["The top account is ", "[", "[PER_", "0001", "]]", " overall."])
    shown += restorer.flush()
    assert shown == "The top account is Priya Raman overall."
    assert "[[" not in shown and "PER_" not in shown


def test_end_to_end_through_the_orchestrator(db, admin, demo_metrics, stub):
    """The full real path: a question goes in, and the provider sees only tokens."""
    from backend.ai import orchestrator

    # Protect a real column in the synthetic catalog so there is something to tokenise.
    sales = db.scalar(select(Table).where(Table.table_name == "sales"))
    col = next(c for c in sales.columns if c.column_name == "region")
    col.anon_strategy, col.anon_entity = "pseudonym", "org"

    db.query(AIProviderConfig).delete()
    db.add(AIProviderConfig(provider="custom", model="test-model", api_key="k", base_url=stub,
                            name="stub", purposes=["reasoning", "planning"], is_default=True))
    conv = Conversation(user_id=admin.id)
    db.add(conv)
    db.commit()

    events = list(orchestrator.run(db, admin, conv, "Why did revenue decline in August 2026?"))
    done = events[-1]

    assert CAPTURED, "the orchestrator never reached the provider"
    bodies = "\n".join(c["body"] for c in CAPTURED)
    for region in ("West", "East", "North", "South"):
        assert f'"{region}"' not in bodies, f"real region {region!r} reached the provider"
    assert "[[ORG_" in bodies, "the model received no tokens at all"

    anon = done["payload"]["anonymization"]
    assert anon["policies_active"] >= 1 and anon["tokens_issued"] >= 1
    assert anon["job_id"]

    col.anon_strategy = None  # leave the shared session-scoped catalog as we found it
    db.commit()


def test_viewer_cannot_read_the_policy_screen(db):
    """The preview column shows the real value for passthrough columns — that is the point of
    it — so the screen must not be reachable by a role that has PII masked everywhere else."""
    from fastapi.testclient import TestClient

    from backend.main import app
    from backend.security.auth import create_token

    client = TestClient(app)
    viewer = db.query(User).filter_by(role="viewer").one()
    analyst = db.query(User).filter_by(role="analyst").one()
    hdr = lambda u: {"Authorization": f"Bearer {create_token(u)}"}  # noqa: E731

    assert client.get("/api/privacy/policies", headers=hdr(viewer)).status_code == 403
    assert client.get("/api/privacy/summary", headers=hdr(viewer)).status_code == 403
    assert client.get("/api/privacy/policies", headers=hdr(analyst)).status_code == 200
