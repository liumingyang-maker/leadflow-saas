from __future__ import annotations

from urllib.parse import quote

import pytest


@pytest.fixture
def isolated_tracking_db(tmp_path):
    import admin_db

    admin_db.DATA_DIR = tmp_path
    admin_db.ADMIN_DB_PATH = tmp_path / "admin.db"
    admin_db.init()
    return admin_db


def _tracking_row(admin_db, tracking_id: str) -> dict:
    with admin_db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM email_tracking WHERE tracking_id=?",
            (tracking_id,),
        ).fetchone()
    return dict(row)


def _tracking_id(admin_db) -> str:
    return admin_db.create_tracking("tenant-a", "lead-a", "Subject")


def test_unsigned_u_parameter_cannot_control_click_redirect(client, isolated_tracking_db):
    tracking_id = _tracking_id(isolated_tracking_db)

    response = client.get(f"/t/c/{tracking_id}?u=https://evil.example/phish")

    assert response.status_code == 400
    assert response.headers.get("Location") is None
    assert "evil.example" not in response.get_data(as_text=True)
    assert _tracking_row(isolated_tracking_db, tracking_id)["click_count"] == 0


def test_signed_http_target_redirects_and_counts_click(client, isolated_tracking_db):
    import web.app as web_app

    tracking_id = _tracking_id(isolated_tracking_db)
    target = "https://buyer.example/products?a=1"
    sig = web_app._click_token(tracking_id, target)

    response = client.get(f"/t/c/{tracking_id}?u={quote(target, safe='')}&sig={sig}")

    assert response.status_code in {302, 303}
    assert response.headers["Location"] == target
    assert _tracking_row(isolated_tracking_db, tracking_id)["click_count"] == 1


@pytest.mark.parametrize(
    "target",
    [
        "file:///etc/passwd",
        "http://localhost/admin",
        "http://127.0.0.1/admin",
        "http://10.0.0.1/admin",
        "http://192.168.1.10/admin",
        "http://[::1]/admin",
    ],
)
def test_signed_unsafe_targets_go_to_safe_page(client, isolated_tracking_db, target):
    import web.app as web_app

    tracking_id = _tracking_id(isolated_tracking_db)
    sig = web_app._click_token(tracking_id, target)

    response = client.get(f"/t/c/{tracking_id}?u={quote(target, safe='')}&sig={sig}")

    assert response.status_code == 400
    assert response.headers.get("Location") is None
    assert _tracking_row(isolated_tracking_db, tracking_id)["click_count"] == 0


def test_tampered_click_target_does_not_redirect_or_count(client, isolated_tracking_db):
    import web.app as web_app

    tracking_id = _tracking_id(isolated_tracking_db)
    good_target = "https://buyer.example/products"
    tampered_target = "https://evil.example/phish"
    sig = web_app._click_token(tracking_id, good_target)

    response = client.get(f"/t/c/{tracking_id}?u={quote(tampered_target, safe='')}&sig={sig}")

    assert response.status_code == 400
    assert response.headers.get("Location") is None
    assert _tracking_row(isolated_tracking_db, tracking_id)["click_count"] == 0


def test_mailer_generates_signed_click_links(isolated_tracking_db):
    from tenant_mailer import TenantMailer

    import web.app as web_app

    tracking_id = _tracking_id(isolated_tracking_db)
    target = "https://buyer.example/products"
    html = TenantMailer({})._inject_tracking(
        f'<a href="{target}">product</a>',
        {
            "click_base": f"https://leadflow.example/t/c/{tracking_id}?u=",
            "click_signer": lambda url: web_app._click_token(tracking_id, url),
        },
    )

    assert f"/t/c/{tracking_id}?u=" in html
    assert "sig=" in html
    assert "evil.example" not in html
