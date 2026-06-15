"""P0-000 重构基线：pytest 全局夹具。

关键点：当前应用在 *import 时* 就会读取 ``DATA_DIR`` / ``SECRET_KEY`` 并启动后台调度
线程（这是现状，后续任务 P1-025 才会移除）。因此必须在导入任何应用模块之前，把环境
指向一个临时目录、提供一个测试用密钥，从而保证：

* 测试只读写临时目录，绝不碰仓库里的真实库 / 租户数据；
* 测试不依赖真实密钥；
* smoke 测试只发 GET，不触发任何外部网络调用。
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEDULER_LOOP_QUALNAMES = {
    "start_followup_scheduler.<locals>._loop",
    "start_radar_scheduler.<locals>._loop",
    "start_auto_backup.<locals>._loop",
}

# ── 必须在任何应用模块被 import 之前完成环境隔离 ──────────────────────────
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="leadflow_test_data_")
os.environ["DATA_DIR"] = _TEST_DATA_DIR  # 所有库/文件落到临时目录
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SITE_URL", "http://localhost")
atexit.register(lambda: shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True))

# 与 serve.py 保持一致的导入路径（web / core 为脚本式命名空间目录）
for _p in (REPO_ROOT, REPO_ROOT / "web", REPO_ROOT / "core"):
    _sp = str(_p)
    if _sp not in sys.path:
        sys.path.insert(0, _sp)


class AppImportThreadGuard:
    """Block only web.app's import-time scheduler threads."""

    def __init__(self) -> None:
        self.blocked: list[str] = []
        self.real_starts: list[str] = []

    def thread_factory(self, original_thread):
        guard = self

        class GuardedThread:
            def __init__(self, *args, **kwargs) -> None:
                target = kwargs.get("target")
                if target is None and args:
                    target = args[0]
                self._target_qualname = getattr(target, "__qualname__", "")
                self._target_module = getattr(target, "__module__", "")
                self._blocked = (
                    self._target_module == "web.app"
                    and self._target_qualname in SCHEDULER_LOOP_QUALNAMES
                )
                if self._blocked:
                    self._thread = None
                    guard.blocked.append(self._target_qualname)
                else:
                    self._thread = original_thread(*args, **kwargs)

            def start(self):
                if self._blocked:
                    return None
                guard.real_starts.append(self._target_qualname)
                return self._thread.start()

            def __getattr__(self, name):
                if self._thread is None:
                    raise AttributeError(name)
                return getattr(self._thread, name)

        return GuardedThread


_APP_IMPORT_THREAD_GUARD = AppImportThreadGuard()


@pytest.fixture(scope="session")
def app_import_thread_guard():
    return _APP_IMPORT_THREAD_GUARD


@pytest.fixture(scope="session")
def flask_app():
    """加载真实应用（已指向临时 DATA_DIR），返回测试配置后的 Flask app。

    CSRF is left ENABLED (WTF_CSRF_ENABLED defaults to True). Tests that issue
    POST/PUT/PATCH/DELETE through a Flask route must obtain a real CSRF token
    via ``get_csrf_token`` / ``csrf_post`` helpers below. This keeps the test
    app faithful to production (P0-002 requirement: tests must not globally
    bypass CSRF).
    """
    import admin_db

    admin_db.init()  # 在临时目录建好全局库（CREATE TABLE IF NOT EXISTS，幂等）

    original_thread = threading.Thread
    threading.Thread = _APP_IMPORT_THREAD_GUARD.thread_factory(original_thread)
    try:
        from web.app import app
    finally:
        threading.Thread = original_thread

    app.config.update(TESTING=True)
    return app


@pytest.fixture
def client(flask_app):
    """Flask 测试客户端（进程内，不开真实端口/不走真实网络）。"""
    return flask_app.test_client()


# ── CSRF helpers (real Flask-WTF token, same session) ─────────────────────
# CSRF is enabled in the test app. Any test that posts through a Flask route
# must fetch a real token with the SAME client/session it then posts with.


def get_csrf_token(client, path: str = "/login") -> str:
    """GET ``path`` with ``client`` and return the real CSRF token rendered in
    its ``<form>``. The token is bound to this client's session, so the same
    client must be used for the subsequent POST. Raises AssertionError if the
    page renders no token (e.g. the path has no CSRF-protected form)."""
    import re

    response = client.get(path)
    assert response.status_code == 200, f"{path} -> {response.status_code}"
    body = response.get_data(as_text=True)
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', body)
    assert match, f"no csrf_token found on {path}"
    return match.group(1)


def csrf_post(client, path, *, data=None, json_body=None, headers=None, token_path=None, **kwargs):
    """POST ``path`` with a real CSRF token, using the same client/session.

    The token is fetched from ``token_path`` (defaults to a page that renders a
    token for this route family) via GET. For JSON/AJAX posts the token is sent
    as the ``X-CSRFToken`` header; for form posts it is added to ``data`` under
    the ``csrf_token`` key. Extra ``headers``/``kwargs`` are preserved."""
    token = get_csrf_token(client, token_path or _token_source_for(path))

    if json_body is not None:
        merged_headers = {"X-CSRFToken": token}
        if headers:
            merged_headers.update(headers)
        return client.post(path, json=json_body, headers=merged_headers, **kwargs)

    merged_data = dict(data or {})
    merged_data.setdefault("csrf_token", token)
    return client.post(path, data=merged_data, headers=headers, **kwargs)


def _token_source_for(path):
    """Pick a GET-accessible page that renders a CSRF token for the same route
    family as ``path``. Most state-changing routes share a page with their own
    GET form, so fall back to ``path`` itself."""
    if path == "/admin/logout":
        # While must_change_password may be set, GET /admin redirects to the
        # change-password page which renders the token.
        return "/admin/change-password"
    return path
