from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

USER_EMAIL = "guard-user@example.com"
USER_PASSWORD = "safe-password"
ADMIN_EMAIL = "guard-admin@example.com"
ADMIN_PASSWORD = "temporary-safe-password-123"


@pytest.fixture(autouse=True)
def enable_csrf_and_clear_rate_limits(flask_app):
    import web.app as web_app

    original = flask_app.config.get("WTF_CSRF_ENABLED")
    with web_app._rl._lock:
        web_app._rl._hits.clear()
    flask_app.config["WTF_CSRF_ENABLED"] = True
    yield
    flask_app.config["WTF_CSRF_ENABLED"] = original
    with web_app._rl._lock:
        web_app._rl._hits.clear()


@pytest.fixture
def isolated_admin_db(tmp_path):
    import admin_db
    import tenant_ctx

    admin_db.DATA_DIR = tmp_path
    admin_db.ADMIN_DB_PATH = tmp_path / "admin.db"
    tenant_ctx.DATA_DIR = tmp_path
    admin_db.init()
    return admin_db


def _csrf_from_page(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.get_data(as_text=True))
    assert match, response.get_data(as_text=True)[:500]
    return match.group(1)


def _create_verified_tenant(admin_db, email: str = USER_EMAIL, password: str = USER_PASSWORD):
    import tenant_ctx

    result = admin_db.register_tenant(email, password)
    assert result["ok"]
    with admin_db.get_conn() as conn:
        conn.execute(
            "UPDATE tenants SET email_verified=1, onboarding_done=1 WHERE id=?",
            (result["tenant_id"],),
        )
    tenant_dir = tenant_ctx.tenant_dir(result["tenant_id"])
    tenant_dir.mkdir(parents=True, exist_ok=True)
    (tenant_dir / "config.json").write_text(
        json.dumps({"onboarding_step": 3}),
        encoding="utf-8",
    )
    return result["tenant_id"]


def _login_user(client, admin_db, email: str = USER_EMAIL):
    tid = _create_verified_tenant(admin_db, email)
    token = _csrf_from_page(client, "/login")
    response = client.post(
        "/login",
        data={"email": email, "password": USER_PASSWORD, "csrf_token": token},
    )
    assert response.status_code in {302, 303}
    return tid


def _login_admin(client, admin_db):
    assert admin_db.create_admin(ADMIN_EMAIL, ADMIN_PASSWORD, must_change_password=False)["ok"]
    token = _csrf_from_page(client, "/admin/login")
    response = client.post(
        "/admin/login",
        data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf_token": token},
    )
    assert response.status_code in {302, 303}


def _past() -> str:
    return (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")


def _future() -> str:
    return (datetime.now(UTC) + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")


def _set_tenant_fields(admin_db, tid: str, **fields) -> None:
    sql = ", ".join(f"{name}=?" for name in fields)
    with admin_db.get_conn() as conn:
        conn.execute(f"UPDATE tenants SET {sql} WHERE id=?", (*fields.values(), tid))


def test_session_cookie_config_is_secure_in_production(monkeypatch):
    import web.app as web_app

    app = Flask("prod-cookie-test")
    monkeypatch.setenv("APP_ENV", "production")

    web_app._configure_session_security(app)

    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.permanent_session_lifetime == timedelta(days=7)


def test_session_cookie_config_allows_http_in_tests(monkeypatch):
    import web.app as web_app

    app = Flask("test-cookie-test")
    monkeypatch.setenv("APP_ENV", "test")

    web_app._configure_session_security(app)

    assert app.config["SESSION_COOKIE_SECURE"] is False
    assert app.permanent_session_lifetime == timedelta(days=7)


def test_proxy_headers_are_not_trusted_by_default(monkeypatch, flask_app):
    import web.app as web_app

    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    with flask_app.test_request_context(
        "/",
        environ_base={"REMOTE_ADDR": "203.0.113.10"},
        headers={"X-Forwarded-For": "198.51.100.99"},
    ):
        assert web_app._client_ip() == "203.0.113.10"


def test_proxyfix_is_opt_in_with_configured_hops(monkeypatch):
    import web.app as web_app

    app = Flask("proxy-test")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")

    applied = web_app._configure_proxy_fix(app)

    assert applied is True
    assert isinstance(app.wsgi_app, ProxyFix)
    assert app.config["TRUST_PROXY_HEADERS"] is True
    assert app.config["TRUSTED_PROXY_HOPS"] == 1


def test_user_login_rotates_away_old_session_identity(client, isolated_admin_db):
    with client.session_transaction() as sess:
        sess["tenant_id"] = "old-tenant"
        sess["is_admin"] = True
        sess["admin_email"] = "old-admin@example.com"

    tid = _login_user(client, isolated_admin_db)

    with client.session_transaction() as sess:
        assert sess["tenant_id"] == tid
        assert sess["tenant_email"] == USER_EMAIL
        assert "is_admin" not in sess
        assert "admin_email" not in sess
        assert sess.permanent is True


def test_admin_login_rotates_away_old_session_identity(client, isolated_admin_db):
    with client.session_transaction() as sess:
        sess["tenant_id"] = "old-tenant"
        sess["tenant_email"] = "old@example.com"
        sess["is_admin"] = True
        sess["admin_email"] = "old-admin@example.com"

    _login_admin(client, isolated_admin_db)

    with client.session_transaction() as sess:
        assert sess["is_admin"] is True
        assert sess["admin_email"] == ADMIN_EMAIL
        assert "tenant_id" not in sess
        assert "tenant_email" not in sess
        assert sess.permanent is True


def test_suspended_tenant_is_blocked_on_next_protected_request(client, isolated_admin_db):
    tid = _login_user(client, isolated_admin_db)
    _set_tenant_fields(isolated_admin_db, tid, status="suspended")

    response = client.get("/workbench")

    assert response.status_code in {302, 303}
    assert "/login" in response.headers["Location"]
    with client.session_transaction() as sess:
        assert "tenant_id" not in sess


def test_trial_expired_tenant_is_redirected_to_upgrade_without_loop(client, isolated_admin_db):
    tid = _login_user(client, isolated_admin_db)
    _set_tenant_fields(isolated_admin_db, tid, status="trial", trial_ends=_past())

    blocked = client.get("/workbench")
    allowed = client.get("/upgrade")

    assert blocked.status_code in {302, 303}
    assert blocked.headers["Location"].endswith("/upgrade")
    assert allowed.status_code == 200


def test_plan_expired_tenant_gets_json_error_but_can_access_upgrade(client, isolated_admin_db):
    tid = _login_user(client, isolated_admin_db)
    _set_tenant_fields(
        isolated_admin_db,
        tid,
        status="active",
        plan="pro",
        plan_expires_at=_past(),
    )
    task = isolated_admin_db.create_task(tid, "collect")

    blocked = client.get(f"/task/{task['id']}", headers={"Accept": "application/json"})
    allowed = client.get("/upgrade")

    assert blocked.status_code == 403
    assert blocked.get_json() == {"ok": False, "error": "subscription_expired"}
    assert allowed.status_code == 200


def test_active_paid_tenant_with_future_expiry_can_access_protected_route(
    client, isolated_admin_db
):
    tid = _login_user(client, isolated_admin_db)
    _set_tenant_fields(
        isolated_admin_db,
        tid,
        status="active",
        plan="pro",
        plan_expires_at=_future(),
    )

    response = client.get("/workbench")

    assert response.status_code == 200


def test_admin_routes_are_not_blocked_by_tenant_guard(client, isolated_admin_db):
    tid = _create_verified_tenant(isolated_admin_db)
    _set_tenant_fields(isolated_admin_db, tid, status="suspended")

    _login_admin(client, isolated_admin_db)
    response = client.get("/admin")

    assert response.status_code == 200
