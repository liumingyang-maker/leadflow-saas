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


# ── Blocking issue 2: GET /email-templates/reset must no longer mutate state ──


def _write_custom_templates(client, isolated_admin_db):
    """Put a marker into the tenant's email_templates.json so we can detect a reset.

    Also marks the tenant's onboarding as complete (onboarding_step >= 3) so the
    onboarding_required-guarded /email-templates routes are reachable."""
    import json as _json

    import tenant_ctx

    tid = isolated_admin_db.get_tenant_by_email(USER_EMAIL)["id"]
    tenant_dir = tenant_ctx.tenant_dir(tid)
    tenant_dir.mkdir(parents=True, exist_ok=True)
    # onboarding_step >= 3 satisfies the onboarding_required guard.
    (tenant_dir / "config.json").write_text(_json.dumps({"onboarding_step": 3}), encoding="utf-8")
    path = tenant_ctx.get_email_templates_path(tid)
    path.write_text(_json.dumps({"first_contact": {"subject": "MARKER"}}), encoding="utf-8")
    return path


def _complete_onboarding(isolated_admin_db):
    import json as _json

    import tenant_ctx

    tid = isolated_admin_db.get_tenant_by_email(USER_EMAIL)["id"]
    tenant_dir = tenant_ctx.tenant_dir(tid)
    tenant_dir.mkdir(parents=True, exist_ok=True)
    (tenant_dir / "config.json").write_text(_json.dumps({"onboarding_step": 3}), encoding="utf-8")
    return tid


def _email_token_used(admin_db, token: str) -> int:
    with admin_db.get_conn() as conn:
        row = conn.execute("SELECT used FROM email_tokens WHERE token=?", (token,)).fetchone()
    assert row is not None
    return int(row["used"])


def _inbound_tokens(admin_db, tid: str) -> list[str]:
    with admin_db.get_conn() as conn:
        rows = conn.execute(
            "SELECT token FROM inbound_tokens WHERE tenant_id=? ORDER BY token",
            (tid,),
        ).fetchall()
    return [row["token"] for row in rows]


def test_email_templates_reset_is_post_only_and_does_not_mutate_on_get(client, isolated_admin_db):
    _login_user(client, isolated_admin_db)
    path = _write_custom_templates(client, isolated_admin_db)

    # GET must not reset (405 now that the route is POST-only).
    get_resp = client.get("/email-templates/reset")
    assert get_resp.status_code == 405
    assert "MARKER" in path.read_text(encoding="utf-8")


def test_email_templates_reset_without_csrf_token_does_not_mutate(client, isolated_admin_db):
    _login_user(client, isolated_admin_db)
    path = _write_custom_templates(client, isolated_admin_db)

    resp = client.post("/email-templates/reset")
    assert resp.status_code == 400
    assert "MARKER" in path.read_text(encoding="utf-8")


def test_email_templates_reset_with_wrong_token_does_not_mutate(client, isolated_admin_db):
    _login_user(client, isolated_admin_db)
    path = _write_custom_templates(client, isolated_admin_db)

    resp = client.post("/email-templates/reset", data={"csrf_token": "wrong"})
    assert resp.status_code == 400
    assert "MARKER" in path.read_text(encoding="utf-8")


def test_email_templates_reset_with_valid_token_resets(client, isolated_admin_db):
    _login_user(client, isolated_admin_db)
    path = _write_custom_templates(client, isolated_admin_db)
    assert "MARKER" in path.read_text(encoding="utf-8")

    token = _csrf_from_page(client, "/email-templates")
    resp = client.post("/email-templates/reset", data={"csrf_token": token})
    assert resp.status_code in {302, 303}
    assert "MARKER" not in path.read_text(encoding="utf-8")


def test_email_templates_reset_url_is_post_in_snapshot():
    snapshot = (REPO_ROOT / "tests" / "snapshots" / "url_map.txt").read_text(encoding="utf-8")
    assert "POST | /email-templates/reset | email_templates_reset" in snapshot
    assert "GET | /email-templates/reset" not in snapshot


def test_no_state_changing_get_link_to_email_reset():
    """The reset control must be a POST form, not a state-changing GET <a href>."""
    tpl = (REPO_ROOT / "web" / "templates" / "app" / "email_tpl.html").read_text(encoding="utf-8")
    # No bare GET link to the reset endpoint...
    assert 'href="/email-templates/reset"' not in tpl
    # ...but there is a POST form with a CSRF token targeting it.
    assert 'action="/email-templates/reset"' in tpl
    assert tpl.count("csrf_field()") >= 1


def test_reset_password_get_and_failed_posts_do_not_consume_token(client, isolated_admin_db):
    tid = _create_verified_tenant(isolated_admin_db)
    token = isolated_admin_db.create_email_token(tid, USER_EMAIL, "reset")
    before_hash = isolated_admin_db.get_tenant_by_email(USER_EMAIL)["password_hash"]

    first_get = client.get(f"/reset-password/{token}")
    second_get = client.get(f"/reset-password/{token}")
    assert first_get.status_code == 200
    assert second_get.status_code == 200
    assert _email_token_used(isolated_admin_db, token) == 0

    missing_csrf = client.post(
        f"/reset-password/{token}",
        data={"password": "new-password-123", "password2": "new-password-123"},
    )
    assert missing_csrf.status_code == 400
    assert _email_token_used(isolated_admin_db, token) == 0

    wrong_csrf = client.post(
        f"/reset-password/{token}",
        data={
            "password": "new-password-123",
            "password2": "new-password-123",
            "csrf_token": "wrong",
        },
    )
    assert wrong_csrf.status_code == 400
    assert _email_token_used(isolated_admin_db, token) == 0

    csrf_token = _csrf_from_page(client, f"/reset-password/{token}")
    mismatch = client.post(
        f"/reset-password/{token}",
        data={
            "password": "new-password-123",
            "password2": "different-password-123",
            "csrf_token": csrf_token,
        },
    )
    assert mismatch.status_code == 200
    assert _email_token_used(isolated_admin_db, token) == 0
    assert isolated_admin_db.get_tenant_by_email(USER_EMAIL)["password_hash"] == before_hash


def test_reset_password_post_consumes_token_only_after_password_update(client, isolated_admin_db):
    tid = _create_verified_tenant(isolated_admin_db)
    token = isolated_admin_db.create_email_token(tid, USER_EMAIL, "reset")
    csrf_token = _csrf_from_page(client, f"/reset-password/{token}")

    changed = client.post(
        f"/reset-password/{token}",
        data={
            "password": "new-password-123",
            "password2": "new-password-123",
            "csrf_token": csrf_token,
        },
    )

    assert changed.status_code in {302, 303}
    assert _email_token_used(isolated_admin_db, token) == 1
    assert not isolated_admin_db.login_tenant(USER_EMAIL, USER_PASSWORD)["ok"]
    assert isolated_admin_db.login_tenant(USER_EMAIL, "new-password-123")["ok"]

    reused = client.get(f"/reset-password/{token}")
    assert reused.status_code == 200
    assert b'name="password"' not in reused.data


def test_reset_password_concurrent_double_submit_uses_token_once(isolated_admin_db, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    tid = _create_verified_tenant(isolated_admin_db)
    token = isolated_admin_db.create_email_token(tid, USER_EMAIL, "reset")
    password_a = "new-password-thread-a-123"
    password_b = "new-password-thread-b-456"
    barrier = threading.Barrier(2)
    original_inspect = isolated_admin_db.inspect_email_token

    def synced_inspect(token_arg: str, token_type: str) -> dict:
        result = original_inspect(token_arg, token_type)
        if token_arg == token and token_type == "reset" and result["ok"]:
            barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(isolated_admin_db, "inspect_email_token", synced_inspect)

    def reset_to(password: str):
        return password, isolated_admin_db.reset_tenant_password_with_token(token, password)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reset_to, [password_a, password_b]))

    successes = [(password, result) for password, result in results if result["ok"]]
    failures = [(password, result) for password, result in results if not result["ok"]]
    assert len(successes) == 1
    assert len(failures) == 1
    assert _email_token_used(isolated_admin_db, token) == 1

    success_password = successes[0][0]
    failed_password = failures[0][0]
    assert isolated_admin_db.login_tenant(USER_EMAIL, success_password)["ok"]
    assert not isolated_admin_db.login_tenant(USER_EMAIL, failed_password)["ok"]


def test_reset_password_used_token_does_not_change_password(isolated_admin_db):
    tid = _create_verified_tenant(isolated_admin_db)
    token = isolated_admin_db.create_email_token(tid, USER_EMAIL, "reset")
    with isolated_admin_db.get_conn() as conn:
        conn.execute("UPDATE email_tokens SET used=1 WHERE token=?", (token,))
    before_hash = isolated_admin_db.get_tenant_by_email(USER_EMAIL)["password_hash"]

    result = isolated_admin_db.reset_tenant_password_with_token(token, "new-password-123")

    assert not result["ok"]
    assert isolated_admin_db.get_tenant_by_email(USER_EMAIL)["password_hash"] == before_hash
    assert not isolated_admin_db.login_tenant(USER_EMAIL, "new-password-123")["ok"]


def test_reset_password_missing_tenant_rolls_back_token_consume(isolated_admin_db):
    missing_tid = "missing-tenant-id"
    token = isolated_admin_db.create_email_token(missing_tid, USER_EMAIL, "reset")

    result = isolated_admin_db.reset_tenant_password_with_token(token, "new-password-123")

    assert not result["ok"]
    assert _email_token_used(isolated_admin_db, token) == 0


def test_inbound_get_does_not_create_token_and_post_requires_csrf(client, isolated_admin_db):
    _login_user(client, isolated_admin_db)
    tid = _complete_onboarding(isolated_admin_db)
    assert _inbound_tokens(isolated_admin_db, tid) == []

    page = client.get("/inbound")
    assert page.status_code == 200
    assert b'data-token-state="missing"' in page.data
    assert _inbound_tokens(isolated_admin_db, tid) == []

    missing_csrf = client.post("/inbound/regenerate")
    assert missing_csrf.status_code == 400
    assert _inbound_tokens(isolated_admin_db, tid) == []

    wrong_csrf = client.post("/inbound/regenerate", data={"csrf_token": "wrong"})
    assert wrong_csrf.status_code == 400
    assert _inbound_tokens(isolated_admin_db, tid) == []

    csrf_token = _csrf_from_page(client, "/inbound")
    created = client.post("/inbound/regenerate", data={"csrf_token": csrf_token})
    assert created.status_code in {302, 303}
    tokens = _inbound_tokens(isolated_admin_db, tid)
    assert len(tokens) == 1

    with_token = client.get("/inbound")
    assert with_token.status_code == 200
    assert tokens[0].encode() in with_token.data


def test_inbound_post_with_valid_csrf_rotates_existing_token(client, isolated_admin_db):
    _login_user(client, isolated_admin_db)
    tid = _complete_onboarding(isolated_admin_db)
    original = isolated_admin_db.regenerate_inbound_token(tid)

    csrf_token = _csrf_from_page(client, "/inbound")
    rotated = client.post("/inbound/regenerate", data={"csrf_token": csrf_token})

    assert rotated.status_code in {302, 303}
    tokens = _inbound_tokens(isolated_admin_db, tid)
    assert len(tokens) == 1
    assert tokens[0] != original


# ── Blocking issue 1: fetch wrapper must not leak the CSRF token cross-origin ──
#
# We have no headless browser JS engine in CI, so these are static structural
# assertions: they pin the contract of the shared _csrf_fetch.html partial and
# prove the three layouts include it (so they cannot drift back to the old
# origin-blind wrapper). A full functional test of the JS lives in manual review.

FETCH_PARTIAL = REPO_ROOT / "web" / "templates" / "_csrf_fetch.html"
LAYOUTS_USING_FETCH = [
    REPO_ROOT / "web" / "templates" / "app" / "base.html",
    REPO_ROOT / "web" / "templates" / "admin" / "panel.html",
    REPO_ROOT / "web" / "templates" / "onboarding" / "base.html",
]


def test_fetch_helper_exists_and_is_included_by_all_three_layouts():
    assert FETCH_PARTIAL.exists(), "shared _csrf_fetch.html partial is missing"
    source = FETCH_PARTIAL.read_text(encoding="utf-8")

    # Same-origin gate is present...
    assert "location.origin" in source
    assert "new URL(" in source
    # ...on the unsafe-method path only...
    assert "POST" in source and "PUT" in source and "PATCH" in source and "DELETE" in source
    # ...and never overwrites a caller-provided token.
    assert "has('X-CSRFToken')" in source
    # Must handle Request objects (resolve target URL from input.url).
    assert "instanceof Request" in source

    for layout in LAYOUTS_USING_FETCH:
        text = layout.read_text(encoding="utf-8")
        assert '{% include "_csrf_fetch.html" %}' in text, (
            f"{layout.name} must include the shared _csrf_fetch.html partial"
        )


def test_fetch_helper_attaches_token_only_same_origin():
    """The helper must branch on isSameOrigin before attaching the token."""
    source = FETCH_PARTIAL.read_text(encoding="utf-8")

    # The token is set ONLY inside the same-origin branch: the line that sets
    # the header must be guarded by the isSameOrigin(input) check.
    assert "isSameOrigin(input)" in source
    assert "headers.set('X-CSRFToken'" in source
    # The same-origin guard resolves the final request URL (string/URL/Request).
    assert "requestTarget" in source


def test_no_origin_blind_fetch_wrapper_remains():
    """None of the three layouts may keep the old origin-blind wrapper inline."""
    for layout in LAYOUTS_USING_FETCH:
        text = layout.read_text(encoding="utf-8")
        # The old pattern attached the token for every unsafe method with no
        # origin check; its defining inline csrfToken() helper must be gone now
        # that the shared partial owns it.
        assert "function csrfToken()" not in text, (
            f"{layout.name} still has an inline origin-blind csrfToken/fetch wrapper"
        )


def test_fetch_helper_supports_request_url_and_string_inputs():
    source = FETCH_PARTIAL.read_text(encoding="utf-8")
    # String/URL inputs go through String(input); Request inputs use input.url.
    assert "String(input)" in source
    assert "input.url" in source
    # GET/HEAD must never receive a token: only UNSAFE methods are gated.
    assert "GET" in source  # the default when no method is supplied


def test_fetch_helper_preserves_request_and_init_headers():
    source = FETCH_PARTIAL.read_text(encoding="utf-8")

    # Request objects can carry caller-provided headers. The wrapper may add a
    # CSRF token for same-origin unsafe requests, but must not discard those
    # headers or overwrite a token the caller already supplied.
    assert "input.headers" in source
    assert "new Headers(input instanceof Request ? input.headers : undefined)" in source
    assert "new Headers(init.headers)" in source
    assert "headers.has('X-CSRFToken')" in source
