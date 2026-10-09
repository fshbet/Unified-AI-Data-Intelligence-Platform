"""All five authentication modes, against a stub that speaks the real wire protocols.

The point of the auth framework is that a connector declares which modes it accepts and
implements none of them. These tests exercise each mode end to end — token exchange, the
interactive handshake, refresh, and the failure paths that are easy to get wrong (a replayed
state, a device flow that is merely pending, an expired refresh token).
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from backend.auth_providers import create_auth, list_auth_modes
from backend.auth_providers.device_code import AuthPending
from backend.auth_providers.service import (
    AuthError,
    complete_interactive,
    get_credential,
    poll_interactive,
    start_interactive,
)
from backend.metadata.models import DataSource, PendingAuth, SourceCredential

STATE: dict = {"pending_polls": 0, "token_calls": 0, "last_form": {}}


class _Stub(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        form = {k: v[0] for k, v in parse_qs(raw).items()}
        STATE["last_form"] = form
        path = urlparse(self.path).path

        if path.endswith("/devicecode"):
            return self._json({"device_code": "DEV-123", "user_code": "ABCD-EFGH", "interval": 1,
                               "expires_in": 600, "verification_uri": "https://example.test/device"})

        if path.endswith("/token"):
            STATE["token_calls"] += 1
            grant = form.get("grant_type", "")
            if "device_code" in grant:
                STATE["pending_polls"] += 1
                if STATE["pending_polls"] == 1:
                    return self._json({"error": "authorization_pending"}, 400)
                if STATE["pending_polls"] == 2:
                    return self._json({"error": "slow_down"}, 400)
                return self._json({"access_token": "device-token", "refresh_token": "r-dev", "expires_in": 3600})
            if grant == "refresh_token":
                if form.get("refresh_token") == "dead":
                    return self._json({"error": "invalid_grant",
                                       "error_description": "Token has been expired or revoked."}, 400)
                return self._json({"access_token": f"refreshed-{STATE['token_calls']}", "expires_in": 3600})
            if grant == "authorization_code":
                return self._json({"access_token": "code-token", "refresh_token": "r-code", "expires_in": 3600})
            if grant == "client_credentials":
                if form.get("client_secret") != "sp-secret":
                    return self._json({"error": "invalid_client",
                                       "error_description": "AADSTS65001: no admin consent"}, 400)
                return self._json({"access_token": "sp-token", "expires_in": 3600})
            if "jwt-bearer" in grant:
                return self._json({"access_token": "google-sa-token", "expires_in": 3600})
        self._json({"error": "not_found"}, 404)

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def stub():
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def _reset():
    STATE.update({"pending_polls": 0, "token_calls": 0, "last_form": {}})


def _source(db, name: str, mode: str, cfg: dict) -> DataSource:
    src = db.query(DataSource).filter_by(name=name).one_or_none()
    if src is None:
        src = DataSource(name=name, type="rest_api", config={})
        db.add(src)
    src.auth_mode, src.auth_config = mode, cfg
    db.query(SourceCredential).filter_by(source_id=src.id).delete()
    db.flush()
    return src


# A throwaway RSA key so the Google service-account path is exercised for real rather than mocked.
@pytest.fixture(scope="module")
def sa_json():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return {"client_email": "sync@proj.iam.gserviceaccount.com", "private_key": pem,
            "token_uri": "https://oauth2.googleapis.com/token"}


# ---------------------------------------------------------------- the five modes
def test_all_five_modes_are_registered():
    assert {m["mode"] for m in list_auth_modes()} == {
        "service_account", "oauth_code", "device_code", "credentials", "ambient"}


def test_credentials_mode_round_trips_each_shape():
    basic = create_auth("credentials", {"username": "u", "password": "p"}).acquire()
    assert basic.kind == "basic" and basic.header()["Authorization"].startswith("Basic ")

    key = create_auth("credentials", {"api_key": "k", "header_name": "X-Token"}).acquire()
    assert key.header() == {"X-Token": "k"}

    dsn = create_auth("credentials", {"dsn": "postgresql://h/db", "username": "ignored"}).acquire()
    assert dsn.kind == "dsn", "a DSN must win over the individual fields"


def test_service_account_google_signs_a_real_jwt(stub, sa_json):
    auth = create_auth("service_account", {"platform": "google", "credentials_json": json.dumps(sa_json),
                                           "token_url": f"{stub}/token", "scopes": "scope-a"})
    cred = auth.acquire()
    assert cred.kind == "bearer" and cred.secret == "google-sa-token"
    assert STATE["last_form"]["assertion"].count(".") == 2, "a signed JWT assertion must be sent"
    assert not cred.is_expired()


def test_service_account_rejects_a_malformed_key_before_any_network_call(stub):
    auth = create_auth("service_account", {"platform": "google", "credentials_json": '{"client_email": "x"}',
                                           "token_url": f"{stub}/token"})
    with pytest.raises(Exception, match="private_key"):
        auth.acquire()
    assert STATE["token_calls"] == 0, "a bad key must fail before contacting the provider"


def test_service_account_microsoft_explains_missing_admin_consent(stub):
    auth = create_auth("service_account", {"platform": "microsoft", "tenant_id": "t", "client_id": "c",
                                           "client_secret": "wrong", "token_url": f"{stub}/token"})
    with pytest.raises(Exception, match="admin consent"):
        auth.acquire()


def test_ambient_reports_what_it_tried_when_nothing_is_available():
    auth = create_auth("ambient", {"allow_integrated": False})
    with pytest.raises(Exception, match="Tried:"):
        auth.acquire()


# ---------------------------------------------------------------- oauth code
def test_oauth_code_uses_pkce_and_a_verified_state(db, stub):
    src = _source(db, "oauth-src", "oauth_code", {
        "platform": "custom", "client_id": "cid", "client_secret": "sec",
        "authorize_url": f"{stub}/authorize", "token_url": f"{stub}/token", "scopes": "s1"})

    start = start_interactive(db, src, redirect_uri="http://127.0.0.1:8010/cb")
    q = parse_qs(urlparse(start.url).query)
    assert q["code_challenge_method"] == ["S256"] and q["code_challenge"], "PKCE is mandatory"
    state = q["state"][0]

    cred = complete_interactive(db, src, {"code": "abc", "state": state}, state)
    assert cred.secret == "code-token" and cred.refresh_token == "r-code"
    assert src.status == "connected"
    # The redirect_uri at the token step must match the authorize step exactly.
    assert STATE["last_form"]["redirect_uri"] == "http://127.0.0.1:8010/cb"
    assert STATE["last_form"]["code_verifier"]


def test_oauth_state_is_single_use_and_unknown_states_are_refused(db, stub):
    src = _source(db, "oauth-replay", "oauth_code", {
        "platform": "custom", "client_id": "cid", "authorize_url": f"{stub}/authorize",
        "token_url": f"{stub}/token"})
    start = start_interactive(db, src, redirect_uri="http://127.0.0.1:8010/cb")
    state = parse_qs(urlparse(start.url).query)["state"][0]
    complete_interactive(db, src, {"code": "abc", "state": state}, state)

    before = STATE["token_calls"]
    with pytest.raises(AuthError, match="Unknown or already-used"):
        complete_interactive(db, src, {"code": "abc", "state": state}, state)
    with pytest.raises(AuthError, match="Unknown or already-used"):
        complete_interactive(db, src, {"code": "abc", "state": "never-issued"}, "never-issued")
    assert STATE["token_calls"] == before, "a bad state must not reach the token endpoint"


def test_expired_pending_authorisation_is_refused(db, stub):
    src = _source(db, "oauth-expired", "oauth_code", {
        "platform": "custom", "client_id": "cid", "authorize_url": f"{stub}/authorize",
        "token_url": f"{stub}/token"})
    start = start_interactive(db, src, redirect_uri="http://127.0.0.1:8010/cb")
    state = parse_qs(urlparse(start.url).query)["state"][0]
    db.get(PendingAuth, state).expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.flush()
    with pytest.raises(AuthError, match="expired"):
        complete_interactive(db, src, {"code": "abc", "state": state}, state)
    assert db.get(PendingAuth, state) is None, "an expired row must still be consumed"


# ---------------------------------------------------------------- device code
def test_device_flow_survives_pending_and_slow_down(db, stub):
    src = _source(db, "device-src", "device_code", {
        "platform": "microsoft", "client_id": "cid",
        "device_url": f"{stub}/devicecode", "token_url": f"{stub}/token"})
    start = start_interactive(db, src)
    assert start.mode == "device" and start.user_code == "ABCD-EFGH"
    state = db.query(PendingAuth).filter_by(source_id=src.id).one().state

    with pytest.raises(AuthPending):
        poll_interactive(db, src, state)
    with pytest.raises(AuthPending) as pending:
        poll_interactive(db, src, state)
    assert pending.value.interval > 1, "slow_down must widen the polling interval"

    cred = poll_interactive(db, src, state)
    assert cred.secret == "device-token" and src.status == "connected"


# ---------------------------------------------------------------- lifecycle
def test_expired_credential_refreshes_transparently(db, stub):
    src = _source(db, "refresh-src", "oauth_code", {
        "platform": "custom", "client_id": "cid", "token_url": f"{stub}/token",
        "authorize_url": f"{stub}/authorize"})
    start = start_interactive(db, src, redirect_uri="http://127.0.0.1:8010/cb")
    state = parse_qs(urlparse(start.url).query)["state"][0]
    complete_interactive(db, src, {"code": "abc", "state": state}, state)

    row = db.get(SourceCredential, src.id)
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db.flush()

    cred = get_credential(db, src)
    assert cred.secret.startswith("refreshed-")
    assert cred.refresh_token == "r-code", "the refresh token must be carried forward"
    assert not cred.is_expired()


def test_concurrent_callers_cause_exactly_one_refresh(db, stub):
    """A 12-table sync must not fire 12 refreshes; providers that rotate the token on use would
    invalidate each other."""
    from backend.core.db import SessionLocal

    src = _source(db, "concurrent-src", "oauth_code", {
        "platform": "custom", "client_id": "cid", "token_url": f"{stub}/token",
        "authorize_url": f"{stub}/authorize"})
    start = start_interactive(db, src, redirect_uri="http://127.0.0.1:8010/cb")
    state = parse_qs(urlparse(start.url).query)["state"][0]
    complete_interactive(db, src, {"code": "abc", "state": state}, state)
    db.get(SourceCredential, src.id).expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db.commit()

    STATE["token_calls"] = 0
    results: list[str] = []

    def worker():
        with SessionLocal() as s:
            results.append(get_credential(s, s.get(DataSource, src.id)).secret)
            s.commit()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    assert STATE["token_calls"] == 1, f"expected one refresh, got {STATE['token_calls']}"
    assert len(set(results)) == 1, "every caller must receive the same new token"


def test_failed_refresh_marks_the_source_needs_auth(db, stub):
    src = _source(db, "dead-refresh", "oauth_code", {
        "platform": "custom", "client_id": "cid", "token_url": f"{stub}/token",
        "authorize_url": f"{stub}/authorize"})
    db.add(SourceCredential(source_id=src.id, kind="bearer", secret_enc="x",
                            refresh_enc=None, expires_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    db.flush()
    from backend.core.crypto import encrypt

    db.get(SourceCredential, src.id).refresh_enc = encrypt("dead")
    db.flush()

    with pytest.raises(AuthError):
        get_credential(db, src)
    assert src.status == "needs_auth", "the UI has to know to offer re-authentication"
    assert src.last_error


def test_credential_repr_never_prints_the_secret():
    """The first unhandled exception would otherwise put a live token in the log."""
    cred = create_auth("credentials", {"api_key": "super-secret-value"}).acquire()
    assert "super-secret-value" not in repr(cred)
    assert "super-secret-value" not in f"{cred}"


def test_connectors_declare_modes_and_existing_sources_default_to_credentials(db):
    from backend.auth_providers import supported_modes_for
    from backend.connectors.registry import get_connector_class

    assert [m["mode"] for m in supported_modes_for(get_connector_class("powerbi"))][0] == "service_account"
    assert "ambient" in [m["mode"] for m in supported_modes_for(get_connector_class("postgresql"))]
    # Backwards compatibility: a source created before this feature keeps working untouched.
    legacy = DataSource(name="legacy-src", type="sqlite", config={"path": "x.db"})
    db.add(legacy)
    db.flush()
    assert legacy.auth_mode == "credentials"
