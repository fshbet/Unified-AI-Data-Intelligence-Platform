"""The unified output API: one contract, one envelope, enforced scoping.

The deliverable is that an external application attaches once and gets everything, so these
tests assert the contract an outside consumer actually depends on — a stable envelope on both
success and failure, provenance on every record, visible staleness, and scoping that cannot be
inferred around.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.main import app
from backend.metadata.models import ApiKey, DataSource, User
from backend.security.auth import create_token

ENVELOPE_KEYS = {"ok", "request_id", "data", "page", "sources", "warnings"}


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def admin_hdr(db):
    return {"Authorization": f"Bearer {create_token(db.query(User).filter_by(role='admin').one())}"}


def _make_key(client, admin_hdr, **body) -> str:
    body.setdefault("name", "test-key")
    body.setdefault("scopes", ["read", "ask"])
    r = client.post("/api/v1/unified/keys", headers=admin_hdr, json=body)
    assert r.status_code == 201, r.text
    return r.json()["key"]


# ---------------------------------------------------------------- keys
def test_key_is_shown_once_and_stored_only_as_a_hash(client, admin_hdr, db):
    raw = _make_key(client, admin_hdr, name="once")
    row = db.scalar(select(ApiKey).where(ApiKey.name == "once"))
    assert row is not None
    assert raw not in (row.key_hash, row.prefix), "the key itself must never be stored"
    assert len(row.key_hash) == 64
    # And it is never echoed back by any later call.
    listed = client.get("/api/v1/unified/keys", headers=admin_hdr).json()
    assert all(raw not in str(k) for k in listed)


def test_unknown_and_revoked_keys_are_refused(client, admin_hdr, db):
    raw = _make_key(client, admin_hdr, name="revoke-me")
    hdr = {"Authorization": f"Bearer {raw}"}
    assert client.get("/api/v1/unified/schema", headers=hdr).status_code == 200

    key_id = db.scalar(select(ApiKey).where(ApiKey.name == "revoke-me")).id
    assert client.delete(f"/api/v1/unified/keys/{key_id}", headers=admin_hdr).status_code == 204
    # Revocation must bite immediately, with no restart and no cache to expire.
    assert client.get("/api/v1/unified/schema", headers=hdr).status_code == 401
    assert client.get("/api/v1/unified/schema",
                      headers={"Authorization": "Bearer edi_live_nonsense"}).status_code == 401
    assert client.get("/api/v1/unified/schema").status_code == 401


# ---------------------------------------------------------------- envelope
def test_every_response_uses_the_same_envelope(client, admin_hdr, demo_metrics):
    raw = _make_key(client, admin_hdr)
    hdr = {"Authorization": f"Bearer {raw}"}

    ok = client.get("/api/v1/unified/records?limit=5", headers=hdr)
    assert ok.status_code == 200 and ENVELOPE_KEYS <= set(ok.json())
    assert ok.json()["ok"] is True

    # Failures carry the same keys, so a consumer writes one parser.
    bad = client.get("/api/v1/unified/records?cursor=not-a-cursor", headers=hdr)
    body = bad.json()["detail"]
    assert bad.status_code == 400 and ENVELOPE_KEYS <= set(body)
    assert body["ok"] is False and body["error"]["code"] == "bad_cursor"

    unauth = client.get("/api/v1/unified/schema", headers={"Authorization": "Bearer nope"})
    assert ENVELOPE_KEYS <= set(unauth.json()["detail"])


# ---------------------------------------------------------------- records
def test_one_request_returns_records_from_every_system_with_provenance(client, admin_hdr, demo_metrics):
    """The deliverable: attach once, get everything, know where each row came from."""
    raw = _make_key(client, admin_hdr)
    body = client.get("/api/v1/unified/records?limit=200",
                      headers={"Authorization": f"Bearer {raw}"}).json()

    records = body["data"]["records"]
    assert records, "no records returned from a fully synced demo catalog"
    for r in records:
        assert r["source"]["name"] and r["source"]["type"]
        assert r["lineage"]["table"] and r["lineage"]["query_ref"].startswith("Q-")
        assert r["entity"] and isinstance(r["fields"], dict)

    assert len({r["source"]["name"] for r in records}) >= 2, "records should span multiple systems"
    # Staleness has to be visible, or "all your data in one place" is a liability.
    assert body["sources"] and all("last_sync_at" in s and "stale" in s for s in body["sources"])


def test_paging_is_opaque_and_does_not_repeat_rows(client, admin_hdr, demo_metrics):
    raw = _make_key(client, admin_hdr)
    hdr = {"Authorization": f"Bearer {raw}"}
    seen, cursor, pages = [], None, 0
    while pages < 5:
        url = f"/api/v1/unified/records?limit=25{f'&cursor={cursor}' if cursor else ''}"
        body = client.get(url, headers=hdr).json()
        seen += [r["id"] for r in body["data"]["records"]]
        cursor = body["page"]["next_cursor"]
        pages += 1
        if not cursor:
            break
    assert seen, "paging returned nothing"
    assert len(seen) == len(set(seen)), "a row was returned on more than one page"
    assert cursor is None or not cursor.isdigit(), "cursors must be opaque, not offsets"


def test_ndjson_streams(client, admin_hdr, demo_metrics):
    raw = _make_key(client, admin_hdr)
    r = client.get("/api/v1/unified/records?limit=10&format=ndjson",
                   headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    assert lines and all(ln.startswith("{") for ln in lines)


# ---------------------------------------------------------------- scoping
def test_a_scoped_key_cannot_reach_other_entities_or_sources(client, admin_hdr, db, demo_metrics):
    src = db.scalars(select(DataSource)).first()
    raw = _make_key(client, admin_hdr, name="scoped", scopes=["read"], sources=[src.name])
    hdr = {"Authorization": f"Bearer {raw}"}

    body = client.get("/api/v1/unified/records?limit=100", headers=hdr).json()
    names = {r["source"]["name"] for r in body["data"]["records"]}
    assert names <= {src.name}, f"a source-scoped key saw {names}"
    assert {s["name"] for s in body["sources"]} <= {src.name}

    entity_key = _make_key(client, admin_hdr, name="entity-scoped", scopes=["read"], entities=["sales"])
    r = client.get("/api/v1/unified/records?entity=hr",
                   headers={"Authorization": f"Bearer {entity_key}"})
    assert r.status_code == 403


def test_total_does_not_leak_counts_the_key_cannot_see(client, admin_hdr, db, demo_metrics):
    """Scoping is applied before the query, not as a post-filter — otherwise `total` reports
    the size of data the caller is not allowed to read."""
    src = db.scalars(select(DataSource)).first()
    scoped = _make_key(client, admin_hdr, name="total-scoped", scopes=["read"], sources=[src.name])
    full = _make_key(client, admin_hdr, name="total-full", scopes=["read"])

    t_scoped = client.get("/api/v1/unified/records?limit=1",
                          headers={"Authorization": f"Bearer {scoped}"}).json()["page"]["total"]
    t_full = client.get("/api/v1/unified/records?limit=1",
                        headers={"Authorization": f"Bearer {full}"}).json()["page"]["total"]
    assert t_scoped <= t_full
    assert t_scoped < t_full or t_full == 0, "the scoped total matched the unscoped one"


def test_scopes_are_enforced_per_endpoint(client, admin_hdr):
    read_only = _make_key(client, admin_hdr, name="read-only", scopes=["read"])
    r = client.post("/api/v1/unified/ask", headers={"Authorization": f"Bearer {read_only}"},
                    json={"question": "anything"})
    assert r.status_code == 403 and r.json()["detail"]["error"]["code"] == "forbidden"


def test_rate_limit_returns_429_with_retry_after(client, admin_hdr):
    raw = _make_key(client, admin_hdr, name="slow", scopes=["read"], rate_limit_per_min=3)
    hdr = {"Authorization": f"Bearer {raw}"}
    codes = [client.get("/api/v1/unified/schema", headers=hdr).status_code for _ in range(5)]
    assert 429 in codes
    limited = client.get("/api/v1/unified/schema", headers=hdr)
    assert limited.status_code == 429 and limited.headers.get("Retry-After")


# ---------------------------------------------------------------- schema + client
def test_schema_describes_every_reachable_table(client, admin_hdr, demo_metrics):
    raw = _make_key(client, admin_hdr)
    data = client.get("/api/v1/unified/schema", headers={"Authorization": f"Bearer {raw}"}).json()["data"]
    assert data["entities"] and data["tables"]
    t = data["tables"][0]
    assert {"table", "entity", "source", "columns"} <= set(t)
    assert all({"name", "type"} <= set(c) for c in t["columns"])


def test_the_embeddable_client_pages_without_the_caller_touching_cursors(client, admin_hdr, demo_metrics, monkeypatch):
    """An external application should attach in three lines."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "client"))
    import edi_client

    raw = _make_key(client, admin_hdr)
    edi = edi_client.EDI("http://testserver", api_key=raw)
    monkeypatch.setattr(edi, "_client", client)  # drive the TestClient transport
    client.headers.update({"Authorization": f"Bearer {raw}"})

    records = list(edi.records(limit=60, page_size=20))
    assert len(records) >= 1
    assert len({r.id for r in records}) == len(records), "the client re-yielded a row"
    first = records[0]
    assert first.source.name and first.lineage.get("table")
