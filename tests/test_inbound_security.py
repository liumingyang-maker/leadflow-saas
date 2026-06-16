from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

USER_EMAIL = "inbound-user@example.com"
USER_PASSWORD = "safe-password"


@pytest.fixture(autouse=True)
def enable_csrf(flask_app):
    original = flask_app.config.get("WTF_CSRF_ENABLED")
    flask_app.config["WTF_CSRF_ENABLED"] = True
    yield
    flask_app.config["WTF_CSRF_ENABLED"] = original


@pytest.fixture
def isolated_admin_db(tmp_path, monkeypatch):
    import admin_db
    import tenant_ctx

    monkeypatch.setattr(admin_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(admin_db, "ADMIN_DB_PATH", tmp_path / "admin.db")
    monkeypatch.setattr(tenant_ctx, "DATA_DIR", tmp_path)
    admin_db.init()
    return admin_db


@pytest.fixture(autouse=True)
def clean_memory_rate_limit(flask_app):
    import web.app as web_app

    with web_app._rl._lock:
        web_app._rl._hits.clear()


def _csrf_from_page(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.get_data(as_text=True))
    assert match, response.get_data(as_text=True)[:500]
    return match.group(1)


def _create_tenant(admin_db, *, email=USER_EMAIL, password=USER_PASSWORD, status="active"):
    import tenant_ctx

    result = admin_db.register_tenant(email, password)
    assert result["ok"]
    tid = result["tenant_id"]
    with admin_db.get_conn() as conn:
        conn.execute(
            """
            UPDATE tenants
               SET email_verified=1, onboarding_done=1, status=?
             WHERE id=?
            """,
            (status, tid),
        )
    tenant_ctx.save_config(tid, {"onboarding_step": 3})
    return tid


def _set_allowed_origins(tid: str, origins: list[str]):
    import tenant_ctx

    cfg = tenant_ctx.load_config(tid)
    cfg["onboarding_step"] = 3
    cfg["inbound_allowed_origins"] = "\n".join(origins)
    tenant_ctx.save_config(tid, cfg)


def _login_user(client, admin_db, *, email=USER_EMAIL, password=USER_PASSWORD):
    _create_tenant(admin_db, email=email, password=password)
    token = _csrf_from_page(client, "/login")
    response = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": token},
    )
    assert response.status_code in {302, 303}
    return admin_db.get_tenant_by_email(email)["id"]


def _regenerate_token(client) -> str:
    import admin_db

    csrf_token = _csrf_from_page(client, "/inbound")
    response = client.post("/inbound/regenerate", data={"csrf_token": csrf_token})
    assert response.status_code in {302, 303}
    with client.session_transaction() as session:
        tid = session["tenant_id"]
    return admin_db.get_inbound_token(tid)


def _post_inbound(client, token: str, payload=None, *, origin=None, headers=None, **kwargs):
    merged = dict(headers or {})
    if origin is not None:
        merged["Origin"] = origin
    return client.post(
        f"/api/inbound/{token}",
        json=payload if payload is not None else _valid_payload(),
        headers=merged,
        **kwargs,
    )


def _valid_payload(**overrides):
    payload = {
        "name": "Ada Buyer",
        "email": "ada@example-importer.com",
        "phone": "+15551234567",
        "company": "Ada Import Co",
        "message": "Please send a catalog.",
        "source": "contact-form",
        "page_url": "https://shop.example.com/contact",
        "referrer": "https://google.example/search",
    }
    payload.update(overrides)
    return payload


def _lead_count(tid: str) -> int:
    import tenant_ctx

    db_path = Path(tenant_ctx.get_db_path(tid))
    if not db_path.exists():
        return 0
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]


def _lead_rows(tid: str) -> list[sqlite3.Row]:
    import tenant_ctx

    with sqlite3.connect(tenant_ctx.get_db_path(tid)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM leads ORDER BY created_at, id").fetchall()


def test_inbound_token_is_strong_encrypted_and_rotates_immediately(client, isolated_admin_db):
    tid = _login_user(client, isolated_admin_db)
    first = _regenerate_token(client)

    assert first.startswith("in_")
    assert len(first) >= 40
    with isolated_admin_db.get_conn() as conn:
        row = conn.execute("SELECT * FROM inbound_tokens WHERE tenant_id=?", (tid,)).fetchone()
    assert row["token"] != first
    assert row["token_digest"]
    assert first not in row["token"]
    assert first not in row["token_ciphertext"]

    second = _regenerate_token(client)
    assert second != first
    assert _post_inbound(client, first).status_code == 404
    assert _post_inbound(client, second).status_code == 201


def test_browser_origin_must_be_allowlisted_but_server_to_server_without_origin_is_allowed(
    client, isolated_admin_db
):
    tid = _login_user(client, isolated_admin_db)
    token = _regenerate_token(client)
    _set_allowed_origins(tid, ["https://shop.example.com"])

    denied = _post_inbound(client, token, origin="https://evil.example")
    assert denied.status_code == 403
    assert denied.get_json()["error"] == "origin_not_allowed"
    assert "Access-Control-Allow-Origin" not in denied.headers

    null_origin = _post_inbound(client, token, origin="null")
    assert null_origin.status_code == 403

    allowed = _post_inbound(client, token, origin="https://shop.example.com")
    assert allowed.status_code == 201
    assert allowed.headers["Access-Control-Allow-Origin"] == "https://shop.example.com"
    assert "Access-Control-Allow-Credentials" not in allowed.headers

    server_to_server = _post_inbound(client, token, _valid_payload(email="next@example.com"))
    assert server_to_server.status_code == 201


def test_inbound_preflight_uses_same_origin_allowlist(client, isolated_admin_db):
    tid = _login_user(client, isolated_admin_db)
    token = _regenerate_token(client)
    _set_allowed_origins(tid, ["https://shop.example.com"])

    allowed = client.options(
        f"/api/inbound/{token}", headers={"Origin": "https://shop.example.com"}
    )
    assert allowed.status_code == 204
    assert allowed.headers["Access-Control-Allow-Origin"] == "https://shop.example.com"
    assert allowed.headers["Access-Control-Allow-Methods"] == "POST, OPTIONS"
    assert "Idempotency-Key" in allowed.headers["Access-Control-Allow-Headers"]

    denied = client.options(f"/api/inbound/{token}", headers={"Origin": "https://evil.example"})
    assert denied.status_code == 403
    assert "Access-Control-Allow-Origin" not in denied.headers


def test_inbound_rejects_large_and_unsupported_requests_without_creating_leads(
    client, isolated_admin_db, monkeypatch
):
    monkeypatch.setenv("INBOUND_MAX_BODY_BYTES", "128")
    tid = _login_user(client, isolated_admin_db)
    token = _regenerate_token(client)

    too_large = _post_inbound(client, token, _valid_payload(message="x" * 300))
    assert too_large.status_code == 413

    unsupported = client.post(
        f"/api/inbound/{token}",
        data=b"email=ada@example.com",
        content_type="text/plain",
    )
    assert unsupported.status_code == 415
    assert _lead_count(tid) == 0


def test_inbound_validates_allowlisted_fields_and_does_not_trust_tenant_id(
    client, isolated_admin_db
):
    tid = _login_user(client, isolated_admin_db)
    token = _regenerate_token(client)

    missing_contact = _post_inbound(client, token, _valid_payload(email="", phone=""))
    assert missing_contact.status_code == 400
    assert missing_contact.get_json()["error"] == "invalid_request"

    bad_email = _post_inbound(client, token, _valid_payload(email="not an email"))
    assert bad_email.status_code == 400

    bad_url = _post_inbound(client, token, _valid_payload(page_url="javascript:alert(1)"))
    assert bad_url.status_code == 400

    bad_control = _post_inbound(client, token, _valid_payload(name="Ada\x00Buyer"))
    assert bad_control.status_code == 400

    ok = _post_inbound(
        client,
        token,
        _valid_payload(
            tenant_id="attacker",
            ignored_field="ignored",
            email="allowed@example.com",
            message="x" * 5000,
        ),
    )
    assert ok.status_code == 201
    rows = _lead_rows(tid)
    assert len(rows) == 1
    assert rows[0]["email"] == "allowed@example.com"
    assert "ignored_field" not in (rows[0]["notes"] or "")


def test_inbound_honeypot_accepts_without_creating_lead(client, isolated_admin_db):
    tid = _login_user(client, isolated_admin_db)
    token = _regenerate_token(client)

    response = _post_inbound(client, token, _valid_payload(_website="bot-filled"))

    assert response.status_code == 202
    assert response.get_json() == {"ok": True, "status": "accepted"}
    assert _lead_count(tid) == 0


def test_inbound_idempotency_key_and_no_key_fingerprint_prevent_duplicates(
    client, isolated_admin_db
):
    tid = _login_user(client, isolated_admin_db)
    token = _regenerate_token(client)
    payload = _valid_payload(email="idem@example.com")

    first = _post_inbound(client, token, payload, headers={"Idempotency-Key": "abc-123"})
    duplicate = _post_inbound(client, token, payload, headers={"Idempotency-Key": "abc-123"})
    conflict = _post_inbound(
        client,
        token,
        _valid_payload(email="different@example.com"),
        headers={"Idempotency-Key": "abc-123"},
    )
    no_key_dup = _post_inbound(client, token, payload)

    assert first.status_code == 201
    assert duplicate.status_code == 200
    assert duplicate.get_json()["status"] == "duplicate"
    assert conflict.status_code == 409
    assert no_key_dup.status_code == 200
    assert _lead_count(tid) == 1


def test_inbound_idempotency_scope_is_tenant_and_token(client, isolated_admin_db, flask_app):
    first_tid = _login_user(client, isolated_admin_db)
    first_token = _regenerate_token(client)

    second_client = flask_app.test_client()
    second_tid = _login_user(
        second_client,
        isolated_admin_db,
        email="second-inbound@example.com",
        password="safe-password-2",
    )
    second_token = _regenerate_token(second_client)

    headers = {"Idempotency-Key": "shared-key"}
    first_response = _post_inbound(
        client, first_token, _valid_payload(email="one@example.com"), headers=headers
    )
    second_response = _post_inbound(
        second_client, second_token, _valid_payload(email="two@example.com"), headers=headers
    )
    assert first_response.status_code == 201
    assert second_response.status_code == 201
    assert _lead_count(first_tid) == 1
    assert _lead_count(second_tid) == 1


def test_inbound_persistent_rate_limits_token_ip_and_tenant(client, isolated_admin_db, monkeypatch):
    monkeypatch.setenv("INBOUND_RATE_TOKEN_IP_LIMIT", "2")
    monkeypatch.setenv("INBOUND_RATE_TENANT_LIMIT", "3")
    monkeypatch.setenv("INBOUND_RATE_WINDOW_SECONDS", "60")
    tid = _login_user(client, isolated_admin_db)
    token = _regenerate_token(client)

    first = _post_inbound(client, token, _valid_payload(email="one@example.com"))
    second = _post_inbound(
        client,
        token,
        _valid_payload(email="two@example.com"),
        headers={"X-Forwarded-For": "203.0.113.200"},
    )
    third = _post_inbound(client, token, _valid_payload(email="three@example.com"))

    assert first.status_code == 201
    assert second.status_code == 201
    assert third.status_code == 429
    assert "Retry-After" in third.headers
    assert _lead_count(tid) == 2
    with isolated_admin_db.get_conn() as conn:
        rows = conn.execute("SELECT scope, count FROM inbound_rate_limits").fetchall()
    assert rows
    assert max(row["count"] for row in rows) >= 2


def test_inbound_suspended_or_missing_tenant_token_is_not_usable(client, isolated_admin_db):
    tid = _login_user(client, isolated_admin_db)
    token = _regenerate_token(client)
    with isolated_admin_db.get_conn() as conn:
        conn.execute("UPDATE tenants SET status='suspended' WHERE id=?", (tid,))

    suspended = _post_inbound(client, token)
    assert suspended.status_code == 404
    assert suspended.get_json()["error"] == "not_found"
    assert _lead_count(tid) == 0
