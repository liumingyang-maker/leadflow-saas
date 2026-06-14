"""P0-000 smoke 测试：确认应用能启动、关键公开页面可访问。

仅发 GET，且页面为服务端 Jinja 渲染，不触发任何外部 API / 网络调用。
"""

from __future__ import annotations


def test_app_startup(flask_app):
    """应用能成功加载并注册了路由。"""
    assert flask_app is not None
    assert len(list(flask_app.url_map.iter_rules())) > 0


def test_app_import_does_not_start_scheduler_threads(flask_app, app_import_thread_guard):
    """Importing the app must not run the three scheduler background loops."""
    assert flask_app is not None
    assert app_import_thread_guard.blocked == [
        "start_followup_scheduler.<locals>._loop",
        "start_radar_scheduler.<locals>._loop",
        "start_auto_backup.<locals>._loop",
    ]
    assert app_import_thread_guard.real_starts == []


def test_landing_page(client):
    """落地页可访问，且渲染出品牌名。"""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "外贸雷达".encode() in resp.data


def test_login_page(client):
    """登录页可访问。"""
    resp = client.get("/login")
    assert resp.status_code == 200


def test_register_page(client):
    """注册页可访问。"""
    resp = client.get("/register")
    assert resp.status_code == 200
