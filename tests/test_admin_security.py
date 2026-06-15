from __future__ import annotations

import sqlite3

import pytest

STRONG_PASSWORD = "temporary-safe-password-123"
NEW_PASSWORD = "new-safe-password-456"
LEGACY_PASSWORD = "admin" + "123"


@pytest.fixture
def clean_admin_db(tmp_path):
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


def _admin_rows(admin_db):
    with admin_db.get_conn() as conn:
        rows = conn.execute("SELECT * FROM admin_users ORDER BY email").fetchall()
    return [dict(row) for row in rows]


def _insert_legacy_default_admin(admin_db):
    with admin_db.get_conn() as conn:
        conn.execute(
            "INSERT INTO admin_users (id, email, password_hash, created_at) VALUES (?,?,?,?)",
            (
                "legacy",
                "admin@leads.com",
                admin_db._hash(LEGACY_PASSWORD),
                admin_db._now(),
            ),
        )


def test_empty_database_init_does_not_create_default_admin(clean_admin_db):
    assert _admin_rows(clean_admin_db) == []


def test_cli_create_admin_hashes_password_and_requires_first_change(
    clean_admin_db, monkeypatch, capsys
):
    from scripts import create_admin

    monkeypatch.setattr("builtins.input", lambda prompt="": "owner@example.com")
    passwords = iter([STRONG_PASSWORD, STRONG_PASSWORD])
    monkeypatch.setattr(create_admin.getpass, "getpass", lambda prompt="": next(passwords))

    assert create_admin.main(["create"]) == 0

    out = capsys.readouterr()
    assert STRONG_PASSWORD not in out.out
    assert STRONG_PASSWORD not in out.err
    rows = _admin_rows(clean_admin_db)
    assert len(rows) == 1
    assert rows[0]["email"] == "owner@example.com"
    assert rows[0]["password_hash"] != STRONG_PASSWORD
    assert rows[0]["password_hash"].startswith("pbkdf2$")
    assert rows[0]["must_change_password"] == 1


@pytest.mark.parametrize("password", [LEGACY_PASSWORD, "short"])
def test_cli_rejects_weak_admin_password(clean_admin_db, monkeypatch, password):
    from scripts import create_admin

    monkeypatch.setattr("builtins.input", lambda prompt="": "owner@example.com")
    passwords = iter([password, password])
    monkeypatch.setattr(create_admin.getpass, "getpass", lambda prompt="": next(passwords))

    assert create_admin.main(["create"]) != 0
    assert _admin_rows(clean_admin_db) == []


def test_cli_rejects_mismatched_password_confirmation(clean_admin_db, monkeypatch):
    from scripts import create_admin

    monkeypatch.setattr("builtins.input", lambda prompt="": "owner@example.com")
    passwords = iter([STRONG_PASSWORD, NEW_PASSWORD])
    monkeypatch.setattr(create_admin.getpass, "getpass", lambda prompt="": next(passwords))

    assert create_admin.main(["create"]) != 0
    assert _admin_rows(clean_admin_db) == []


def test_cli_rejects_duplicate_admin_email(clean_admin_db, monkeypatch):
    from scripts import create_admin

    assert clean_admin_db.create_admin("owner@example.com", STRONG_PASSWORD)["ok"]
    monkeypatch.setattr("builtins.input", lambda prompt="": "owner@example.com")
    passwords = iter([NEW_PASSWORD, NEW_PASSWORD])
    monkeypatch.setattr(create_admin.getpass, "getpass", lambda prompt="": next(passwords))

    assert create_admin.main(["create"]) != 0
    assert len(_admin_rows(clean_admin_db)) == 1


def test_cli_reset_password_changes_only_existing_admin(clean_admin_db, monkeypatch):
    from scripts import create_admin

    first = clean_admin_db.create_admin("first@example.com", STRONG_PASSWORD)
    second = clean_admin_db.create_admin("second@example.com", STRONG_PASSWORD)
    assert first["ok"]
    assert second["ok"]
    before_second = clean_admin_db.get_admin_by_email("second@example.com")["password_hash"]

    monkeypatch.setattr("builtins.input", lambda prompt="": "first@example.com")
    passwords = iter([NEW_PASSWORD, NEW_PASSWORD])
    monkeypatch.setattr(create_admin.getpass, "getpass", lambda prompt="": next(passwords))

    assert create_admin.main(["reset-password"]) == 0
    assert clean_admin_db.authenticate_admin("first@example.com", NEW_PASSWORD)["ok"]
    assert clean_admin_db.get_admin_by_email("second@example.com")["password_hash"] == before_second


def test_existing_default_admin_with_old_password_is_blocked(clean_admin_db):
    _insert_legacy_default_admin(clean_admin_db)

    result = clean_admin_db.authenticate_admin("admin@leads.com", LEGACY_PASSWORD)

    assert not result["ok"]
    assert result["error_code"] == "weak_default_password"


def test_existing_default_admin_with_changed_password_can_login(clean_admin_db):
    assert clean_admin_db.create_admin(
        "admin@leads.com", STRONG_PASSWORD, must_change_password=False
    )["ok"]

    result = clean_admin_db.authenticate_admin("admin@leads.com", STRONG_PASSWORD)

    assert result["ok"]
    assert result["admin"]["email"] == "admin@leads.com"


def test_legacy_admin_wrong_password_uses_generic_login_failure(clean_admin_db, client):
    _insert_legacy_default_admin(clean_admin_db)

    response = client.post(
        "/admin/login",
        data={"email": "admin@leads.com", "password": "definitely-wrong-password"},
    )

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "账号或密码错误" in body
    assert "CLI" not in body
    assert "旧默认" not in body
    assert "弱密码" not in body
    with client.session_transaction() as sess:
        assert not sess.get("is_admin")
        assert "admin_must_change_password" not in sess


def test_legacy_admin_correct_old_password_is_blocked_without_session(clean_admin_db, client):
    _insert_legacy_default_admin(clean_admin_db)

    response = client.post(
        "/admin/login",
        data={"email": "admin@leads.com", "password": LEGACY_PASSWORD},
    )

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "CLI" in body
    assert "重置密码" in body
    with client.session_transaction() as sess:
        assert not sess.get("is_admin")
        assert "admin_must_change_password" not in sess

    blocked = client.get("/admin")
    assert blocked.status_code == 302
    assert blocked.headers["Location"].endswith("/admin/login")


def test_changed_default_email_admin_can_login_through_route(clean_admin_db, client):
    assert clean_admin_db.create_admin(
        "admin@leads.com", STRONG_PASSWORD, must_change_password=False
    )["ok"]

    response = client.post(
        "/admin/login",
        data={"email": "admin@leads.com", "password": STRONG_PASSWORD},
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/admin")
    assert "CLI" not in response.get_data(as_text=True)
    with client.session_transaction() as sess:
        assert sess.get("is_admin") is True
        assert sess.get("admin_email") == "admin@leads.com"
        assert sess.get("admin_must_change_password") is False


def test_old_admin_table_migration_is_repeatable(clean_admin_db, tmp_path):
    clean_admin_db.ADMIN_DB_PATH = tmp_path / "legacy-admin.db"
    with sqlite3.connect(clean_admin_db.ADMIN_DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE admin_users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO admin_users (id, email, password_hash, created_at) VALUES (?,?,?,?)",
            ("existing", "owner@example.com", clean_admin_db._hash(STRONG_PASSWORD), "now"),
        )

    clean_admin_db.init()
    clean_admin_db.init()

    row = clean_admin_db.get_admin_by_email("owner@example.com")
    assert row["must_change_password"] == 0
    assert clean_admin_db.authenticate_admin("owner@example.com", STRONG_PASSWORD)["ok"]


def test_first_login_requires_password_change(clean_admin_db, client, monkeypatch):
    from scripts import create_admin

    monkeypatch.setattr("builtins.input", lambda prompt="": "owner@example.com")
    passwords = iter([STRONG_PASSWORD, STRONG_PASSWORD])
    monkeypatch.setattr(create_admin.getpass, "getpass", lambda prompt="": next(passwords))
    assert create_admin.main(["create"]) == 0

    login = client.post(
        "/admin/login",
        data={"email": "owner@example.com", "password": STRONG_PASSWORD},
    )
    assert login.status_code == 302
    assert login.headers["Location"].endswith("/admin/change-password")

    blocked = client.get("/admin")
    assert blocked.status_code == 302
    assert blocked.headers["Location"].endswith("/admin/change-password")

    deep_blocked = client.get("/admin/pipeline")
    assert deep_blocked.status_code == 302
    assert deep_blocked.headers["Location"].endswith("/admin/change-password")

    same = client.post(
        "/admin/change-password",
        data={
            "password": STRONG_PASSWORD,
            "password2": STRONG_PASSWORD,
        },
    )
    assert same.status_code == 200
    assert clean_admin_db.get_admin_by_email("owner@example.com")["must_change_password"] == 1

    changed = client.post(
        "/admin/change-password",
        data={"password": NEW_PASSWORD, "password2": NEW_PASSWORD},
    )
    assert changed.status_code == 302
    assert changed.headers["Location"].endswith("/admin")
    assert clean_admin_db.get_admin_by_email("owner@example.com")["must_change_password"] == 0

    client.post("/admin/logout")
    old_login = client.post(
        "/admin/login",
        data={"email": "owner@example.com", "password": STRONG_PASSWORD},
    )
    assert old_login.status_code == 200

    new_login = client.post(
        "/admin/login",
        data={"email": "owner@example.com", "password": NEW_PASSWORD},
    )
    assert new_login.status_code == 302
    assert new_login.headers["Location"].endswith("/admin")

    allowed = client.get("/admin/pipeline")
    assert allowed.status_code == 200
