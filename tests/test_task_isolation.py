from __future__ import annotations

import json
import re
import uuid

import pytest

USER_A = "task-a@example.com"
USER_B = "task-b@example.com"
PASSWORD = "safe-password"
SECRET_MARKER = "sk-test-secret"


@pytest.fixture(autouse=True)
def enable_csrf(flask_app):
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


class _FailStartThread(_NoStartThread):
    def start(self):
        raise RuntimeError(f"cannot start C:/private/admin.db with {SECRET_MARKER}")


def _task_by_id(admin_db, task_id: str) -> dict | None:
    with admin_db.get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def _set_config(tenant_id: str, cfg: dict) -> None:
    import tenant_ctx

    tenant_dir = tenant_ctx.tenant_dir(tenant_id)
    tenant_dir.mkdir(parents=True, exist_ok=True)
    (tenant_dir / "config.json").write_text(
        json.dumps({"onboarding_step": 3, **cfg}),
        encoding="utf-8",
    )


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

    with isolated_admin_db.get_conn() as conn:
        columns = conn.execute("PRAGMA table_info(tasks)").fetchall()
        indexes = conn.execute("PRAGMA index_list(tasks)").fetchall()
    required = {
        "id",
        "tenant_id",
        "task_type",
        "status",
        "progress",
        "error_message",
        "result_json",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
    }
    assert required <= {row["name"] for row in columns}
    assert any(row["name"] == "idx_tasks_tenant_id_id" for row in indexes)

    with pytest.raises(ValueError):
        isolated_admin_db.create_task("", "collect")


def test_task_repository_enforces_tenant_on_reads_and_updates(isolated_admin_db):
    tenant_a = _create_verified_tenant(isolated_admin_db, USER_A)
    tenant_b = _create_verified_tenant(isolated_admin_db, USER_B)
    task = isolated_admin_db.create_task(tenant_a, "collect")

    assert isolated_admin_db.get_task_for_tenant(task["id"], tenant_a) is not None
    assert isolated_admin_db.get_task_for_tenant(task["id"], tenant_b) is None
    assert isolated_admin_db.get_task_for_tenant("missing", tenant_a) is None

    assert not isolated_admin_db.update_task(task["id"], tenant_b, status="running", progress=20)
    assert isolated_admin_db.update_task(task["id"], tenant_a, status="running", progress=20)
    assert not isolated_admin_db.update_task(
        uuid.uuid4().hex, tenant_a, status="running", progress=20
    )
    assert _task_count(isolated_admin_db) == 1
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
    assert isolated_admin_db.update_task(task["id"], tenant_id, progress=-5)
    assert isolated_admin_db.get_task_for_tenant(task["id"], tenant_id)["progress"] == 0


def test_task_repository_rejects_obvious_terminal_state_regression(isolated_admin_db):
    tenant_id = _create_verified_tenant(isolated_admin_db, USER_A)
    task = isolated_admin_db.create_task(tenant_id, "collect")

    assert isolated_admin_db.update_task(task["id"], tenant_id, status="succeeded")

    assert not isolated_admin_db.update_task(task["id"], tenant_id, status="running")
    assert not isolated_admin_db.update_task(task["id"], tenant_id, status="failed")
    stored = isolated_admin_db.get_task_for_tenant(task["id"], tenant_id)
    assert stored["status"] == "succeeded"


def test_task_repository_sanitizes_failure_messages(isolated_admin_db):
    tenant_id = _create_verified_tenant(isolated_admin_db, USER_A)
    task = isolated_admin_db.create_task(tenant_id, "collect")

    assert isolated_admin_db.update_task(
        task["id"],
        tenant_id,
        status="failed",
        result_log=[f"traceback C:/private/admin.db token={SECRET_MARKER}"],
        error_message=f"traceback C:/private/admin.db token={SECRET_MARKER}",
    )

    stored = isolated_admin_db.get_task_for_tenant(task["id"], tenant_id)
    payload = isolated_admin_db.task_status_payload(stored)
    combined = json.dumps(payload, ensure_ascii=False) + (stored["error_message"] or "")
    assert SECRET_MARKER not in combined
    assert "admin.db" not in combined
    assert "C:/private" not in combined


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
    assert "collect" not in cross.get_data(as_text=True)
    assert tenant_a not in cross.get_data(as_text=True)


def test_task_status_route_ignores_forged_tenant_inputs_and_requires_login(
    client, flask_app, isolated_admin_db
):
    tenant_a = _login(client, isolated_admin_db, USER_A)
    task = isolated_admin_db.create_task(tenant_a, "collect")
    isolated_admin_db.update_task(task["id"], tenant_a, status="succeeded", result_log=["ok"])

    anonymous = flask_app.test_client()
    unauthenticated = anonymous.get(f"/task/{task['id']}")
    assert unauthenticated.status_code in {302, 303, 401}

    other = flask_app.test_client()
    tenant_b = _login(other, isolated_admin_db, USER_B)
    assert tenant_b != tenant_a

    forged_query = other.get(f"/task/{task['id']}?tenant_id={tenant_a}")
    assert forged_query.status_code == 404
    assert forged_query.get_json() == {"ok": False, "error": "task_not_found"}


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

    bad_csrf = client.post(
        "/run/collect",
        json={"channels": []},
        headers={"X-CSRFToken": "bad-token"},
    )
    assert bad_csrf.status_code == 400
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
    assert stored["tenant_id"] != "forged"

    status = client.get(f"/task/{task_id}")
    assert status.status_code == 200
    assert status.get_json()["status"] == "queued"


def test_run_route_ignores_forged_tenant_id_in_json_and_form(
    client, monkeypatch, isolated_admin_db
):
    import web.app as web_app

    tid = _login(client, isolated_admin_db, USER_A)
    monkeypatch.setattr(web_app.threading, "Thread", _NoStartThread)
    token = _csrf_from_page(client, "/collect")

    json_response = client.post(
        "/run/score",
        json={"tenant_id": "forged"},
        headers={"X-CSRFToken": token},
    )
    form_response = client.post(
        "/run/enrich",
        data={"tenant_id": "forged", "csrf_token": token},
    )
    all_response = client.post(
        "/run/all",
        json={"tenant_id": "forged", "channels": []},
        headers={"X-CSRFToken": token},
    )

    for response in (json_response, form_response, all_response):
        assert response.status_code == 200
        task_id = response.get_json()["task_id"]
        assert isolated_admin_db.get_task_for_tenant(task_id, tid) is not None
        assert isolated_admin_db.get_task_for_tenant(task_id, "forged") is None


def test_radar_run_creates_tenant_owned_task(client, flask_app, monkeypatch, isolated_admin_db):
    import web.app as web_app

    tid = _login(client, isolated_admin_db, USER_A)
    _set_config(tid, {"deepseek_api_key": "test-key"})
    monkeypatch.setattr(web_app.threading, "Thread", _NoStartThread)

    token = _csrf_from_page(client, "/collect")
    response = client.post(
        "/radar/run",
        json={"auto_search": True, "sources": ["website"]},
        headers={"X-CSRFToken": token},
    )

    assert response.status_code == 200
    task_id = response.get_json()["task_id"]
    stored = isolated_admin_db.get_task_for_tenant(task_id, tid)
    assert stored is not None
    assert stored["tenant_id"] == tid
    assert stored["task_type"] == "radar"

    other = flask_app.test_client()
    _login(other, isolated_admin_db, USER_B)
    cross = other.get(f"/task/{task_id}")
    assert cross.status_code == 404
    assert cross.get_json() == {"ok": False, "error": "task_not_found"}


def test_thread_start_failure_marks_task_failed_without_sensitive_error(
    client, monkeypatch, isolated_admin_db
):
    import web.app as web_app

    tid = _login(client, isolated_admin_db, USER_A)
    monkeypatch.setattr(web_app.threading, "Thread", _FailStartThread)
    token = _csrf_from_page(client, "/collect")

    response = client.post(
        "/run/collect",
        json={"channels": []},
        headers={"X-CSRFToken": token},
    )

    assert response.status_code == 500
    body = response.get_json()
    assert body["ok"] is False
    assert body["error"] == "task_start_failed"
    task_id = body["task_id"]

    stored = isolated_admin_db.get_task_for_tenant(task_id, tid)
    assert stored["status"] == "failed"
    assert stored["error_message"]
    combined = json.dumps(body, ensure_ascii=False) + stored["error_message"]
    assert SECRET_MARKER not in combined
    assert "admin.db" not in combined
    assert "C:/private" not in combined
    assert _task_by_id(isolated_admin_db, task_id)["tenant_id"] == tid
