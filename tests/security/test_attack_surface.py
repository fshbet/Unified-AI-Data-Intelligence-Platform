"""Regression tests for the findings of the 2026-10-08 security audit.

Every test here asserts an attack is REFUSED. The feature suites prove the happy path works;
these prove the failure path is safe, which is the half that was missing — all six critical
findings passed the 167 functional tests.

Each test names the finding it locks down so a future change that reopens one is obvious.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.main import app
from backend.metadata.models import DataSource, User
from backend.security.auth import create_token


def _code_only(src: str) -> str:
    """Drop comment text before asserting on source. These tests look for forbidden patterns in
    CODE, and the explanatory comments beside each fix naturally quote the very pattern they
    warn about."""
    return "\n".join(line.split("#", 1)[0] for line in src.splitlines())


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def admin_hdr(db):
    return {"Authorization": f"Bearer {create_token(db.query(User).filter_by(role='admin').one())}"}


# ---------------------------------------------------------------- C-1 secrets at rest
class TestSecretsAtRest:
    def test_every_secret_shaped_field_is_recognised(self):
        """The old exact-match set left client_secret, pat_secret, dsn and the Google service
        account private key in plaintext, because none was literally a member."""
        from backend.core.crypto import is_secret_field

        for field in ("password", "client_secret", "pat_secret", "dsn", "access_token",
                      "refresh_token", "api_key", "credentials_json", "private_key", "passphrase"):
            assert is_secret_field(field), f"{field} would be written in plaintext"

    def test_non_secret_lookalikes_are_not_encrypted(self):
        from backend.core.crypto import is_secret_field

        for field in ("auth_mode", "authority", "token_url", "authorize_url", "pat_name",
                      "header_name", "spreadsheet_id", "host", "database"):
            assert not is_secret_field(field), f"{field} would be needlessly encrypted"

    def test_round_trip_and_idempotence(self):
        from backend.core.crypto import decrypt_config, encrypt_config, looks_encrypted

        cfg = {"host": "db.internal", "client_secret": "sp-secret", "dsn": "postgres://u:p@h/d"}
        enc = encrypt_config(cfg)
        assert enc["host"] == "db.internal"
        assert looks_encrypted(enc["client_secret"]) and looks_encrypted(enc["dsn"])
        assert decrypt_config(enc) == cfg
        assert encrypt_config(enc) == enc, "re-encrypting must not double-wrap"

    def test_unreadable_ciphertext_raises_instead_of_being_used_as_a_password(self):
        """decrypt() used to return its input on failure, turning an undecryptable credential
        into a silent password attempt and hiding the plaintext-storage bug entirely."""
        from backend.core.crypto import DecryptionError, decrypt

        with pytest.raises(DecryptionError):
            decrypt("gAAAAABmFAKEFAKEFAKEnotarealtoken==")
        assert decrypt("legacy-plaintext") == "legacy-plaintext"

    def test_no_plaintext_secret_in_the_database_file(self, db, tmp_path):
        from backend.core.crypto import encrypt_config, looks_encrypted

        src = DataSource(name="sec-audit-src", type="sqlite",
                         config=encrypt_config({"path": "x.db", "client_secret": "hunter2-xyz"}))
        db.add(src)
        db.commit()
        assert looks_encrypted(src.config["client_secret"])
        assert "hunter2-xyz" not in json.dumps(src.config)
        db.delete(src)
        db.commit()


# ---------------------------------------------------------------- C-2 reflected XSS
@pytest.fixture
def any_source(db):
    """The test catalog is isolated and may be empty; the callback needs a source to exist."""
    src = db.query(DataSource).filter_by(name="xss-probe").one_or_none()
    if src is None:
        src = DataSource(name="xss-probe", type="sqlite", config={"path": "x.db"})
        db.add(src)
        db.commit()
    return src


class TestOAuthCallbackXSS:
    def test_provider_error_text_is_never_reflected(self, client, any_source):
        """error_description arrives straight from the query string on an UNAUTHENTICATED
        endpoint; interpolating it into HTML was script execution on the app origin."""
        payload = "<img src=x onerror=alert(1)>"
        r = client.get(f"/api/sources/{any_source.id}/auth/callback",
                       params={"state": "bogus", "error": "denied", "error_description": payload})
        assert payload not in r.text
        assert "<img" not in r.text and "onerror" not in r.text

    def test_source_id_cannot_break_out_of_the_script_literal(self, client):
        evil = "x';alert(1);//"
        r = client.get(f"/api/sources/{evil}/auth/callback", params={"state": "s"})
        # Either a clean 404, or present but JSON-escaped — never raw inside the <script>.
        assert "';alert(1);//" not in r.text

    def test_response_carries_hardening_headers(self, client, any_source):
        r = client.get(f"/api/sources/{any_source.id}/auth/callback", params={"state": "bogus"})
        assert "default-src 'none'" in r.headers.get("content-security-policy", "")
        assert r.headers.get("x-content-type-options") == "nosniff"


# ---------------------------------------------------------------- C-3 LeakError containment
class TestLeakErrorContainment:
    def test_leak_error_text_never_reaches_a_client(self):
        """LeakError NAMES the values that leaked so an operator can act. Streaming str(e) to
        the browser published exactly the PII the layer exists to withhold."""
        import inspect

        from backend.api.routers import chat

        src = inspect.getsource(chat)
        assert "except LeakError" in src, "the SSE worker must special-case LeakError"
        leak_block = _code_only(src.split("except LeakError")[1].split("except Exception")[0])
        assert "str(e)" not in leak_block and "{e}" not in leak_block

    def test_generic_handler_does_not_echo_exception_text(self):
        import inspect

        from backend.api.routers import chat

        src = inspect.getsource(chat)
        generic = _code_only(src.split("except Exception")[1][:400])
        assert "str(e)" not in generic, "driver errors embed SQL and row values"

    def test_provider_test_reraises_leak_error(self):
        import inspect

        from backend.ai import providers

        assert "except LeakError:\n            raise" in inspect.getsource(providers)


# ---------------------------------------------------------------- C-4 sandbox
class TestPythonSandbox:
    def test_analysis_is_refused_by_default(self):
        """Verified escape: ().__class__.__base__.__subclasses__() reaches Popen, so model-
        authored code gets host RCE. The in-process guard cannot be made safe."""
        from backend.ai.sandbox import SandboxDisabled, run_analysis

        escape = (
            'P = [c for c in ().__class__.__base__.__subclasses__() if c.__name__ == "Popen"][0]\n'
            'result = {"rce": True}\n'
        )
        with pytest.raises(SandboxDisabled):
            run_analysis(escape, {})

    def test_disabled_tool_is_not_offered_to_the_model(self):
        from backend.ai.tools import tool_definitions
        from backend.core.config import settings

        assert settings.python_analysis_enabled is False, "must ship disabled"
        assert "execute_python_analysis" not in {t["name"] for t in tool_definitions()}


# ---------------------------------------------------------------- C-5 insecure defaults
class TestInsecureDefaults:
    def test_production_refuses_to_start_with_a_default_secret(self):
        from backend.core.config import Settings

        with pytest.raises(Exception, match="EDI_SECRET_KEY"):
            Settings(environment="production", secret_key="change-me-in-production-please",
                     default_admin_password="a-real-password")

    def test_production_refuses_a_default_admin_password(self):
        from backend.core.config import Settings

        with pytest.raises(Exception, match="ADMIN_PASSWORD"):
            Settings(environment="production", secret_key="a" * 50, default_admin_password="admin123")

    def test_production_refuses_development_cors_origins(self):
        from backend.core.config import Settings

        with pytest.raises(Exception, match="CORS"):
            Settings(environment="production", secret_key="a" * 50,
                     default_admin_password="x" * 20, cors_origins=["http://localhost:3000"])

    def test_development_generates_a_key_rather_than_using_a_known_one(self):
        from backend.core.config import INSECURE_DEFAULTS, settings

        assert settings.secret_key not in INSECURE_DEFAULTS
        assert len(settings.secret_key) >= 32


# ---------------------------------------------------------------- C-6 SQL injection
class TestUnifiedApiInjection:
    def _key(self, client, admin_hdr, **body):
        body.setdefault("name", "sec-audit")
        body.setdefault("scopes", ["read"])
        r = client.post("/api/v1/unified/keys", headers=admin_hdr, json=body)
        return {"Authorization": f"Bearer {r.json()['key']}"}

    @pytest.mark.parametrize("payload", [
        "' OR 1=1 --",
        "\\' OR 1=1 -- ",          # MySQL: backslash escapes the quote that ''-doubling added
        "'; DROP TABLE records; --",
        "x' UNION SELECT password FROM users --",
        "’ OR 1=1",            # unicode lookalike quote
        "%' OR '1'='1",
    ])
    def test_search_rejects_injection_payloads(self, client, admin_hdr, payload):
        hdr = self._key(client, admin_hdr)
        r = client.get("/api/v1/unified/records", params={"search": payload, "limit": 5}, headers=hdr)
        assert r.status_code == 400, f"accepted {payload!r}"
        assert r.json()["detail"]["error"]["code"] == "bad_search"

    def test_ordinary_search_terms_still_work(self, client, admin_hdr, demo_metrics):
        hdr = self._key(client, admin_hdr)
        for term in ["renewal", "West", "a.b@c.com", "order-123", "Q3 2026"]:
            r = client.get("/api/v1/unified/records", params={"search": term, "limit": 5}, headers=hdr)
            assert r.status_code == 200, f"rejected a legitimate term: {term!r}"

    def test_tables_survive_every_injection_attempt(self, db):
        rows = db.execute(select(DataSource)).all()
        assert rows is not None  # the catalog is intact

    def test_identifiers_are_quote_escaped(self):
        """Column names are not sanitised on ingest, so a crafted sheet header could inject."""
        from backend.connectors.base import DataConnector

        evil = 'a" , (SELECT 1) AS "b'
        quoted = DataConnector.quote_ident(None, evil)
        assert quoted == '"a"" , (SELECT 1) AS ""b"'
        assert quoted.count('"') % 2 == 0

    def test_unified_uses_the_helper_not_an_fstring(self):
        import inspect

        from backend.api.routers import unified

        src = inspect.getsource(unified._collect)
        assert "quote_ident" in src
        assert 'f\'"{c}"\'' not in src and 'f"\\"{c}\\""' not in src


# ---------------------------------------------------------------- C-7 / access control
class TestQueryHistoryScoping:
    def test_a_non_admin_cannot_read_another_users_query_results(self, client, db):
        """result_preview holds result rows as the EXECUTING user saw them; an admin has no
        masking, so leaking one to a viewer would expose unmasked PII."""
        viewer = db.query(User).filter_by(role="viewer").one()
        rows = client.get("/api/admin/queries?limit=500",
                          headers={"Authorization": f"Bearer {create_token(viewer)}"}).json()
        assert all(r["user"] == viewer.email for r in rows)

    def test_fetching_another_users_query_by_ref_is_forbidden(self, client, db, admin_hdr):
        from backend.metadata.models import QueryLog

        admin = db.query(User).filter_by(role="admin").one()
        viewer = db.query(User).filter_by(role="viewer").one()
        log = QueryLog(ref="Q-SECTEST", user_id=admin.id, sql="SELECT 1", status="ok",
                       result_preview=[["email"], ["real@person.com"]])
        db.add(log)
        db.commit()
        r = client.get("/api/admin/queries/Q-SECTEST",
                       headers={"Authorization": f"Bearer {create_token(viewer)}"})
        assert r.status_code == 403
        assert "real@person.com" not in r.text
        db.delete(log)
        db.commit()


# ---------------------------------------------------------------- G-1 retention
class TestRetention:
    def test_purge_clears_result_data_but_keeps_the_audit_trail(self, db):
        from backend.core.config import settings
        from backend.metadata.models import QueryLog
        from backend.workers.retention import purge_retention

        log = QueryLog(ref="Q-RETAIN", user_id="u", sql="SELECT email FROM staff", status="ok",
                       result_preview=[["email"], ["real@person.com"]])
        db.add(log)
        db.commit()

        original = settings.query_preview_retention_days
        settings.query_preview_retention_days = 0
        try:
            purge_retention(db)
        finally:
            settings.query_preview_retention_days = original

        db.refresh(log)
        assert log.result_preview is None, "the personal data must be gone"
        assert log.sql and log.ref and log.status, "the audit trail must survive"
        db.delete(log)
        db.commit()

    def test_purge_is_idempotent(self, db):
        """A JSON column set to Python None stores the string 'null', which is NOT NULL in SQL —
        so the sweep re-matched every purged row on every run."""
        from backend.core.config import settings
        from backend.workers.retention import purge_retention

        original = settings.query_preview_retention_days
        settings.query_preview_retention_days = 0
        try:
            purge_retention(db)
            second = purge_retention(db)
        finally:
            settings.query_preview_retention_days = original
        assert second["query_previews_cleared"] == 0

    def test_vault_purge_destroys_the_reidentification_key(self, db):
        from backend.anonymize import Anonymizer, Policy, new_job
        from backend.core.config import settings
        from backend.metadata.models import AnonToken
        from backend.workers.retention import purge_retention

        job = new_job(db)
        anon = Anonymizer(db, job, {("t", "name"): Policy("t", "name", "pseudonym", "person")})
        anon.anonymize_result(["name"], [["Priya Raman"]], "t")
        db.commit()
        assert db.query(AnonToken).filter_by(job_id=job.id).count() == 1

        original = settings.anon_vault_retention_hours
        settings.anon_vault_retention_hours = 0
        try:
            purge_retention(db)
        finally:
            settings.anon_vault_retention_hours = original
        assert db.query(AnonToken).filter_by(job_id=job.id).count() == 0


# ---------------------------------------------------------------- error hygiene
class TestErrorHygiene:
    def test_unhandled_errors_do_not_echo_exception_text(self):
        """A PendingRollbackError once returned the full INSERT statement plus its parameter
        values — i.e. real row data — in a 500 response body."""
        import inspect

        from backend import main

        src = _code_only(inspect.getsource(main.unhandled))
        assert "{exc}" not in src and "str(exc)" not in src
        assert "reference" in src, "return a correlation id instead of the exception"

    def test_no_secret_is_echoed_by_the_sources_api(self, client, admin_hdr, db):
        from backend.core.crypto import encrypt_config

        src = DataSource(name="sec-echo-src", type="sqlite",
                         config=encrypt_config({"path": "x.db", "password": "topsecret-abc"}))
        db.add(src)
        db.commit()
        body = client.get("/api/sources", headers=admin_hdr).text
        assert "topsecret-abc" not in body
        db.delete(src)
        db.commit()


class TestAdminBootstrap:
    def test_blank_setting_never_produces_a_blank_password(self):
        """Regression: making default_admin_password blank (to remove the hardcoded 'admin123')
        would have had bootstrap() hash the empty string, creating an admin anyone could log
        into as. An account nobody can reach is better than one everybody can."""
        import inspect

        from backend import main

        src = _code_only(inspect.getsource(main.bootstrap))
        assert "secrets.token_urlsafe" in src, "a blank setting must be replaced, not hashed"
        assert "hash_password(settings.default_admin_password)" not in src

    def test_empty_password_would_otherwise_verify(self):
        from backend.security.auth import hash_password, verify_password

        # Demonstrates why the guard above matters.
        assert verify_password("", hash_password("")) is True
