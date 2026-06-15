from __future__ import annotations

import json
import re
import uuid

import pytest

USER_A = "task-a@example.com"
USER_B = "task-b@example.com"
PASSWORD = "safe-password"


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


def _csrf_from_page(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', body)
    assert match, body[:500]
    return match.group(1)


def _create_verified_tenant(admin_db, email: str) -> str:
    import tenant_ctx

    result = admin_db.register_tenant(email, PASSWORD)
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


def _login(client, admin_db, email: str) -> str:
    tid = _create_verified_tenant(admin_db, email)
    token = _csrf_from_page(client, "/login")
    response = client.post(
        "/login",
        data={"email": email, "password": PASSWORD, "csrf_token": token},
    )
    assert response.status_code in {302, 303}
    return tid


def _task_count(admin_db) -> int:
    with admin_db.get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()
    return int(row["n"])


class _NoStartThread:
    def __init__(self, *args, **kwargs):
        self.target = kwargs.get("target") or (args[0] if args else None)

    def start(self):
        return None


def test_task_repository_creates_uuid_tenant_owned_persistent_tasks(isolated_admin_db):
    tid = _create_verified_tenant(isolated_admin_db, USER_A)

    first = isolated_admin_db.create_task(tid, "collect")
    second = isolated_admin_db.create_task(tid, "collect")

    uuid.UUID(first["id"])
    uuid.UUID(second["id"])
    assert first["id"] != second["id"]
    assert tid not in first["id"]

    stored = isolated_admin_db.get_task_for_tenant(first["id"], tid)
    assert stored is not None
    assert stored["tenant_id"] == tid
    assert stored["status"] == "queued"

    isolated_admin_db.init()
    assert isolated_admin_db.get_task_for_tenant(first["id"], tid)["id"] == first["id"]


def test_task_repository_enforces_tenant_on_reads_and_updates(isolated_admin_db):
    tenant_a = _create_verified_tenant(isolated_admin_db, USER_A)
    tenant_b = _create_verified_tenant(isolated_admin_db, USER_B)
    task = isolated_admin_db.create_task(tenant_a, "collect")

    assert isolated_admin_db.get_task_for_tenant(task["id"], tenant_a) is not None
    assert isolated_admin_db.get_task_for_tenant(task["id"], tenant_b) is None
    assert isolated_admin_db.get_task_for_tenant("missing", tenant_a) is None

    assert not isolated_admin_db.update_task(task["id"], tenant_b, status="running", progress=20)
    assert isolated_admin_db.update_task(task["id"], tenant_a, status="running", progress=20)
    stored = isolated_admin_db.get_task_for_tenant(task["id"], tenant_a)
    assert stored["status"] == "running"
    assert stored["progress"] == 20


def test_task_repository_rejects_invalid_status_and_clamps_progress(isolated_admin_db):
    tenant_id = _create_verified_tenant(isolated_admin_db, USER_A)
    task = isolated_admin_db.create_task(tenant_id, "collect")

    with pytest.raises(ValueError):
        isolated_admin_db.update_task(task["id"], tenant_id, status="surprise")

    assert isolated_admin_db.update_task(task["id"], tenant_id, progress=150)
    assert isolated_admin_db.get_task_for_tenant(task["id"], tenant_id)["progress"] == 100


def test_task_status_route_hides_cross_tenant_tasks(client, flask_app, isolated_admin_db):
    tenant_a = _login(client, isolated_admin_db, USER_A)
    task = isolated_admin_db.create_task(tenant_a, "collect")
    isolated_admin_db.update_task(task["id"], tenant_a, status="succeeded", result_log=["ok"])

    own = client.get(f"/task/{task['id']}")
    assert own.status_code == 200
    assert own.get_json()["status"] == "done"

    other = flask_app.test_client()
    _login(other, isolated_admin_db, USER_B)
    cross = other.get(f"/task/{task['id']}")
    missing = other.get(f"/task/{uuid.uuid4().hex}")

    assert cross.status_code == 404
    assert missing.status_code == 404
    assert cross.get_json() == {"ok": False, "error": "task_not_found"}
    assert missing.get_json() == {"ok": False, "error": "task_not_found"}


def test_run_route_creates_persistent_task_and_requires_csrf(
    client, monkeypatch, isolated_admin_db
):
    import web.app as web_app

    tid = _login(client, isolated_admin_db, USER_A)
    monkeypatch.setattr(web_app.threading, "Thread", _NoStartThread)
    before = _task_count(isolated_admin_db)

    no_csrf = client.post("/run/collect", json={"channels": []})
    assert no_csrf.status_code == 400
    assert _task_count(isolated_admin_db) == before

    token = _csrf_from_page(client, "/collect")
    response = client.post(
        "/run/collect",
        json={"channels": []},
        headers={"X-CSRFToken": token},
    )

    assert response.status_code == 200
    task_id = response.get_json()["task_id"]
    uuid.UUID(task_id)
    stored = isolated_admin_db.get_task_for_tenant(task_id, tid)
    assert stored is not None
    assert stored["tenant_id"] == tid
    assert stored["task_type"] == "collect"

    status = client.get(f"/task/{task_id}")
    assert status.status_code == 200
    assert status.get_json()["status"] == "queued"
