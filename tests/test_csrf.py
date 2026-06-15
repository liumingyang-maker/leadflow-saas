from __future__ import annotations

import re
from pathlib import Path

import pytest

USER_EMAIL = "csrf-user@example.com"
USER_PASSWORD = "safe-password"
ADMIN_EMAIL = "csrf-admin@example.com"
ADMIN_PASSWORD = "temporary-safe-password-123"
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def enable_csrf(flask_app):
    original = flask_app.config.get("WTF_CSRF_ENABLED")
    flask_app.config["WTF_CSRF_ENABLED"] = True
    yield
    flask_app.config["WTF_CSRF_ENABLED"] = original


@pytest.fixture
def isolated_admin_db(tmp_path):
    import admin_db

    admin_db.DATA_DIR = tmp_path
    admin_db.ADMIN_DB_PATH = tmp_path / "admin.db"
    admin_db.init()
    return admin_db


@pytest.fixture(autouse=True)
def clean_rate_limit_state(flask_app):
    import web.app as web_app

    with web_app._rl._lock:
        web_app._rl._hits.clear()


def _csrf_from_page(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', body)
    assert match, body[:500]
    return match.group(1)


def _create_verified_tenant(admin_db, email: str = USER_EMAIL, password: str = USER_PASSWORD):
    result = admin_db.register_tenant(email, password)
    assert result["ok"]
    with admin_db.get_conn() as conn:
        conn.execute(
            "UPDATE tenants SET email_verified=1, onboarding_done=1 WHERE id=?",
            (result["tenant_id"],),
        )
    return result["tenant_id"]


def _login_user(client, admin_db):
    _create_verified_tenant(admin_db)
    token = _csrf_from_page(client, "/login")
    response = client.post(
        "/login",
        data={"email": USER_EMAIL, "password": USER_PASSWORD, "csrf_token": token},
    )
    assert response.status_code in {302, 303}


def _login_admin(client, admin_db):
    assert admin_db.create_admin(ADMIN_EMAIL, ADMIN_PASSWORD, must_change_password=False)["ok"]
    token = _csrf_from_page(client, "/admin/login")
    response = client.post(
        "/admin/login",
        data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf_token": token},
    )
    assert response.status_code in {302, 303}


def test_protected_post_without_token_returns_400(client, isolated_admin_db):
    response = client.post(
        "/login",
        data={"email": USER_EMAIL, "password": USER_PASSWORD},
    )

    assert response.status_code == 400
    assert "安全校验失败".encode() in response.data


def test_protected_post_with_wrong_token_returns_400(client, isolated_admin_db):
    response = client.post(
        "/login",
        data={"email": USER_EMAIL, "password": USER_PASSWORD, "csrf_token": "wrong"},
    )

    assert response.status_code == 400


def test_valid_token_reaches_original_login_flow(client, isolated_admin_db):
    _create_verified_tenant(isolated_admin_db)
    token = _csrf_from_page(client, "/login")

    response = client.post(
        "/login",
        data={"email": USER_EMAIL, "password": USER_PASSWORD, "csrf_token": token},
    )

    assert response.status_code in {302, 303}


def test_token_from_other_session_is_rejected(flask_app, isolated_admin_db):
    first = flask_app.test_client()
    second = flask_app.test_client()
    token = _csrf_from_page(first, "/login")

    response = second.post(
        "/login",
        data={"email": USER_EMAIL, "password": USER_PASSWORD, "csrf_token": token},
    )

    assert response.status_code == 400


def test_get_does_not_require_csrf_token(client):
    response = client.get("/login")

    assert response.status_code == 200


def test_csrf_failure_does_not_execute_register_write(client, isolated_admin_db):
    response = client.post(
        "/register",
        data={
            "email": USER_EMAIL,
            "password": USER_PASSWORD,
            "password2": USER_PASSWORD,
        },
    )

    assert response.status_code == 400
    assert isolated_admin_db.get_tenant_by_email(USER_EMAIL) is None


def test_json_write_without_header_gets_stable_csrf_error(client, isolated_admin_db):
    response = client.post(
        "/product-profile/generate",
        json={"description": "auto parts"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 400
    assert response.get_json() == {"ok": False, "error": "csrf_failed"}


def test_user_login_and_logout_require_valid_csrf(client, isolated_admin_db):
    _login_user(client, isolated_admin_db)

    get_logout = client.get("/logout")
    assert get_logout.status_code == 405
    with client.session_transaction() as session_data:
        assert session_data.get("tenant_id")

    no_token = client.post("/logout")
    assert no_token.status_code == 400

    token = _csrf_from_page(client, "/login")
    logged_out = client.post("/logout", data={"csrf_token": token})
    assert logged_out.status_code in {302, 303}
    with client.session_transaction() as session_data:
        assert "tenant_id" not in session_data


def test_admin_login_change_password_and_logout_require_csrf(client, isolated_admin_db):
    assert isolated_admin_db.create_admin(ADMIN_EMAIL, ADMIN_PASSWORD)["ok"]

    no_token = client.post(
        "/admin/login",
        data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert no_token.status_code == 400

    token = _csrf_from_page(client, "/admin/login")
    logged_in = client.post(
        "/admin/login",
        data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "csrf_token": token},
    )
    assert logged_in.status_code in {302, 303}
    assert logged_in.headers["Location"].endswith("/admin/change-password")

    no_change_token = client.post(
        "/admin/change-password",
        data={"password": "new-safe-password-456", "password2": "new-safe-password-456"},
    )
    assert no_change_token.status_code == 400

    change_token = _csrf_from_page(client, "/admin/change-password")
    changed = client.post(
        "/admin/change-password",
        data={
            "password": "new-safe-password-456",
            "password2": "new-safe-password-456",
            "csrf_token": change_token,
        },
    )
    assert changed.status_code in {302, 303}

    assert client.get("/admin/logout").status_code == 405
    logout_token = _csrf_from_page(client, "/admin")
    logged_out = client.post("/admin/logout", data={"csrf_token": logout_token})
    assert logged_out.status_code in {302, 303}


def test_admin_write_route_is_csrf_protected(client, isolated_admin_db):
    _login_admin(client, isolated_admin_db)

    response = client.post("/admin/tenant/create", data={"email": "new@example.com"})

    assert response.status_code == 400


def test_ajax_write_requires_csrf_header_and_accepts_valid_header(client, isolated_admin_db):
    _login_admin(client, isolated_admin_db)

    missing = client.post(
        "/admin/mail-test",
        json={"to": "not-an-email"},
        headers={"Accept": "application/json"},
    )
    assert missing.status_code == 400
    assert missing.get_json() == {"ok": False, "error": "csrf_failed"}

    token = _csrf_from_page(client, "/admin")
    valid = client.post(
        "/admin/mail-test",
        json={"to": "not-an-email"},
        headers={"Accept": "application/json", "X-CSRFToken": token},
    )
    assert valid.status_code == 400
    assert valid.get_json()["ok"] is False


def test_public_inbound_api_is_csrf_exempt_but_still_requires_token(client):
    response = client.post(
        "/api/inbound/not-a-real-token",
        json={"email": "buyer@example.com"},
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "invalid token"


def test_payment_notify_is_csrf_exempt_but_still_uses_provider_verification(client):
    response = client.post("/pay/notify/xunhupay", data={})

    assert response.status_code == 400
    assert response.get_data(as_text=True) == "fail"


def test_unsubscribe_post_is_csrf_exempt_because_token_is_signed(client, flask_app):
    from web import app as web_app

    token = web_app._unsub_token("tenant-id", "buyer@example.com")
    response = client.post(f"/u/{token}")

    assert response.status_code == 200
    assert b"csrf_failed" not in response.data


def test_template_post_forms_include_csrf_helper():
    template_root = REPO_ROOT / "web" / "templates"
    missing: list[str] = []
    form_pattern = re.compile(
        r"<form\b[^>]*method=[\"']?POST[\"']?[^>]*>.*?</form>",
        re.IGNORECASE | re.DOTALL,
    )

    for template in template_root.rglob("*.html"):
        text = template.read_text(encoding="utf-8")
        for form in form_pattern.findall(text):
            if "csrf_field()" not in form:
                missing.append(str(template.relative_to(REPO_ROOT)))

    assert missing == []


def test_csrf_exemptions_stay_narrow_and_documented():
    source = (REPO_ROOT / "web" / "app.py").read_text(encoding="utf-8")

    assert source.count("@csrf.exempt") == 3
    assert "Public site widget API" in source
    assert "Payment provider callback" in source
    assert "Public unsubscribe endpoint" in source
