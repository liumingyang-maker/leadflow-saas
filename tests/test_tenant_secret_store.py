from __future__ import annotations

import json
import re

import pytest

USER_EMAIL = "secret-user@example.com"
USER_PASSWORD = "safe-password"
SECRET_VALUE = "sk-test-secret-value-1234"


@pytest.fixture(autouse=True)
def enable_csrf_and_clear_rate_limits(flask_app, monkeypatch):
    import web.app as web_app

    monkeypatch.setenv("TENANT_SECRET_KEY", "test-tenant-secret-key")
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


def _create_verified_tenant(admin_db, email: str = USER_EMAIL) -> str:
    import tenant_ctx

    result = admin_db.register_tenant(email, USER_PASSWORD)
    assert result["ok"]
    with admin_db.get_conn() as conn:
        conn.execute(
            "UPDATE tenants SET email_verified=1, onboarding_done=1 WHERE id=?",
            (result["tenant_id"],),
        )
    tenant_dir = tenant_ctx.tenant_dir(result["tenant_id"])
    tenant_dir.mkdir(parents=True, exist_ok=True)
    tenant_ctx.save_config(result["tenant_id"], {"onboarding_step": 3})
    return result["tenant_id"]


def _login_user(client, admin_db) -> str:
    tid = _create_verified_tenant(admin_db)
    token = _csrf_from_page(client, "/login")
    response = client.post(
        "/login",
        data={"email": USER_EMAIL, "password": USER_PASSWORD, "csrf_token": token},
    )
    assert response.status_code in {302, 303}
    return tid


def _raw_config(tenant_id: str) -> str:
    import tenant_ctx

    return (tenant_ctx.tenant_dir(tenant_id) / "config.json").read_text(encoding="utf-8")


def test_save_config_encrypts_tenant_secret_fields(isolated_admin_db):
    import tenant_ctx

    tid = _create_verified_tenant(isolated_admin_db)

    tenant_ctx.save_config(
        tid,
        {
            "onboarding_step": 3,
            "smtp_pass": "smtp-secret-1234",
            "deepseek_api_key": SECRET_VALUE,
            "company_name": "Example Co",
        },
    )

    raw = _raw_config(tid)
    assert "smtp-secret-1234" not in raw
    assert SECRET_VALUE not in raw
    assert "enc:v1:" in raw

    loaded = tenant_ctx.load_config(tid)
    assert loaded["smtp_pass"] == "smtp-secret-1234"
    assert loaded["deepseek_api_key"] == SECRET_VALUE
    assert loaded["company_name"] == "Example Co"


def test_old_plaintext_secret_is_migrated_when_saved(isolated_admin_db):
    import tenant_ctx

    tid = _create_verified_tenant(isolated_admin_db)
    path = tenant_ctx.tenant_dir(tid) / "config.json"
    path.write_text(
        json.dumps({"onboarding_step": 3, "hunter_api_key": SECRET_VALUE}),
        encoding="utf-8",
    )

    cfg = tenant_ctx.load_config(tid)
    assert cfg["hunter_api_key"] == SECRET_VALUE
    tenant_ctx.save_config(tid, cfg)

    raw = _raw_config(tid)
    assert SECRET_VALUE not in raw
    assert tenant_ctx.load_config(tid)["hunter_api_key"] == SECRET_VALUE


def test_wrong_secret_key_fails_with_clear_error(isolated_admin_db, monkeypatch):
    import tenant_ctx

    tid = _create_verified_tenant(isolated_admin_db)
    tenant_ctx.save_config(tid, {"onboarding_step": 3, "smtp_pass": SECRET_VALUE})

    monkeypatch.setenv("TENANT_SECRET_KEY", "different-tenant-secret-key")
    with pytest.raises(tenant_ctx.SecretStoreError, match="Unable to decrypt tenant secret"):
        tenant_ctx.load_config(tid)


def test_previous_secret_key_allows_rotation_on_next_save(isolated_admin_db, monkeypatch):
    import tenant_ctx

    tid = _create_verified_tenant(isolated_admin_db)
    monkeypatch.setenv("TENANT_SECRET_KEY", "old-tenant-secret-key")
    tenant_ctx.save_config(tid, {"onboarding_step": 3, "smtp_pass": SECRET_VALUE})
    encrypted_with_old_key = _raw_config(tid)

    monkeypatch.setenv("TENANT_SECRET_KEY", "new-tenant-secret-key")
    monkeypatch.setenv("TENANT_SECRET_KEY_PREVIOUS", "old-tenant-secret-key")
    cfg = tenant_ctx.load_config(tid)
    assert cfg["smtp_pass"] == SECRET_VALUE

    tenant_ctx.save_config(tid, cfg)
    encrypted_with_new_key = _raw_config(tid)
    assert encrypted_with_new_key != encrypted_with_old_key
    assert SECRET_VALUE not in encrypted_with_new_key

    monkeypatch.delenv("TENANT_SECRET_KEY_PREVIOUS")
    assert tenant_ctx.load_config(tid)["smtp_pass"] == SECRET_VALUE


def test_settings_page_masks_saved_secrets(client, isolated_admin_db):
    import tenant_ctx

    tid = _login_user(client, isolated_admin_db)
    cfg = tenant_ctx.load_config(tid)
    cfg["deepseek_api_key"] = SECRET_VALUE
    tenant_ctx.save_config(tid, cfg)

    response = client.get("/settings")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert SECRET_VALUE not in body
    assert SECRET_VALUE[-4:] in body


def test_settings_empty_secret_submission_preserves_existing_secret(client, isolated_admin_db):
    import tenant_ctx

    tid = _login_user(client, isolated_admin_db)
    cfg = tenant_ctx.load_config(tid)
    cfg["smtp_pass"] = SECRET_VALUE
    tenant_ctx.save_config(tid, cfg)
    token = _csrf_from_page(client, "/settings")

    response = client.post(
        "/settings",
        data={
            "csrf_token": token,
            "mail_channel": "smtp",
            "smtp_pass": "",
            "verify_before_send": "on",
        },
    )

    assert response.status_code == 200
    assert tenant_ctx.load_config(tid)["smtp_pass"] == SECRET_VALUE
    assert SECRET_VALUE not in _raw_config(tid)
