"""
admin_db.py — 全局管理数据库
存储所有租户账号、订阅状态
"""
import sqlite3
import uuid
import hashlib
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

ADMIN_DB_PATH = Path(__file__).parent / "admin.db"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_conn():
    conn = sqlite3.connect(str(ADMIN_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tenants (
                id              TEXT PRIMARY KEY,
                email           TEXT UNIQUE NOT NULL,
                password_hash   TEXT NOT NULL,
                company_name    TEXT DEFAULT '',
                industry        TEXT DEFAULT '',
                status          TEXT DEFAULT 'trial',
                plan            TEXT DEFAULT 'basic',
                created_at      TEXT,
                last_login      TEXT,
                trial_ends      TEXT,
                onboarding_done INTEGER DEFAULT 0,
                note            TEXT DEFAULT '',
                email_verified  INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS admin_users (
                id            TEXT PRIMARY KEY,
                email         TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS email_tokens (
                token       TEXT PRIMARY KEY,
                tenant_id   TEXT,
                email       TEXT,
                type        TEXT,
                created_at  TEXT,
                expires_at  TEXT,
                used        INTEGER DEFAULT 0
            );
        """)
        # 迁移：给老账号补上 email_verified 列（已有账号直接标记为已验证）
        try:
            conn.execute("ALTER TABLE tenants ADD COLUMN email_verified INTEGER DEFAULT 0")
            conn.execute("UPDATE tenants SET email_verified=1")
        except Exception:
            pass
    _ensure_admin()


def _hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _ensure_admin():
    """确保至少有一个管理员账号（默认 admin@leads.com / admin123）"""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM admin_users LIMIT 1").fetchone()
        if not row:
            conn.execute(
                "INSERT INTO admin_users (id, email, password_hash, created_at) VALUES (?,?,?,?)",
                (str(uuid.uuid4()), "admin@leads.com", _hash("admin123"), _now())
            )


# ── 租户操作 ──────────────────────────────────────────────

def register_tenant(email: str, password: str) -> dict:
    """注册新租户，返回 {ok, tenant_id, error}"""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM tenants WHERE email=?", (email,)
        ).fetchone()
        if existing:
            return {"ok": False, "error": "该邮箱已注册"}
        tid = str(uuid.uuid4()).replace("-", "")[:16]
        trial_ends = (datetime.now(timezone.utc) + timedelta(days=14)
                      ).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            INSERT INTO tenants
            (id, email, password_hash, status, created_at, trial_ends)
            VALUES (?,?,?,?,?,?)
        """, (tid, email, _hash(password), "trial", _now(), trial_ends))
    # 创建租户目录
    tenant_dir = Path(__file__).parent / "tenants" / tid
    tenant_dir.mkdir(parents=True, exist_ok=True)
    _init_tenant_config(tid)
    return {"ok": True, "tenant_id": tid}


def _init_tenant_config(tid: str):
    """创建租户默认配置文件"""
    import json
    cfg_path = Path(__file__).parent / "tenants" / tid / "config.json"
    if not cfg_path.exists():
        default = {
            "company_name": "",
            "industry": "",
            "product_name": "",
            "product_desc": "",
            "hs_codes": [],
            "target_countries": [],
            "search_keywords": [],
            "market_priority": {"tier1": [], "tier2": [], "tier3": []},
            "smtp_user": "",
            "smtp_pass": "",
            "importyeti_api_key": "",
            "serpapi_key": "",
            "hunter_api_key": "",
            "deepseek_api_key": "",
            "anthropic_api_key": "",
            "ai_enabled": False,
            "email_ai_mode": False,
            "email_from_name": "",
            "email_signature": "",
            "sender_name": "",
            "onboarding_step": 0
        }
        cfg_path.write_text(json.dumps(default, ensure_ascii=False, indent=2),
                            encoding="utf-8")


def login_tenant(email: str, password: str) -> dict:
    """验证租户登录，返回 {ok, tenant, error}"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE email=?", (email,)
        ).fetchone()
    if not row:
        return {"ok": False, "error": "邮箱或密码错误"}
    if row["password_hash"] != _hash(password):
        return {"ok": False, "error": "邮箱或密码错误"}
    if row["status"] == "suspended":
        return {"ok": False, "error": "账号已被暂停，请联系客服"}
    # 更新最后登录时间
    with get_conn() as conn:
        conn.execute("UPDATE tenants SET last_login=? WHERE id=?",
                     (_now(), row["id"]))
    return {"ok": True, "tenant": dict(row)}


def login_admin(email: str, password: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM admin_users WHERE email=? AND password_hash=?",
            (email, _hash(password))
        ).fetchone()
    return bool(row)


def get_tenant_by_email(email: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tenants WHERE email=?", (email,)).fetchone()
    return dict(row) if row else None


def get_tenant(tid: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tenants WHERE id=?", (tid,)).fetchone()
    return dict(row) if row else {}


def update_tenant(tid: str, **kwargs):
    allowed = {"company_name", "industry", "status", "plan",
               "onboarding_done", "note", "last_login"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    sql = ", ".join(f"{k}=?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE tenants SET {sql} WHERE id=?",
                     list(fields.values()) + [tid])


def all_tenants() -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tenants ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ── 邮箱验证 & 密码重置 Token ─────────────────────────────

def create_email_token(tid: str, email: str, token_type: str) -> str:
    """创建验证/重置 token，有效期30分钟"""
    token = uuid.uuid4().hex
    expires = (datetime.now(timezone.utc) + timedelta(minutes=30)
               ).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO email_tokens (token, tenant_id, email, type, created_at, expires_at)
            VALUES (?,?,?,?,?,?)
        """, (token, tid, email, token_type, _now(), expires))
    return token


def verify_email_token(token: str, token_type: str) -> dict:
    """验证 token，返回 {ok, tenant_id, email, error}"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM email_tokens WHERE token=? AND type=?", (token, token_type)
        ).fetchone()
    if not row:
        return {"ok": False, "error": "链接无效"}
    if row["used"]:
        return {"ok": False, "error": "链接已使用"}
    if row["expires_at"] < _now():
        return {"ok": False, "error": "链接已过期，请重新发送"}
    with get_conn() as conn:
        conn.execute("UPDATE email_tokens SET used=1 WHERE token=?", (token,))
    return {"ok": True, "tenant_id": row["tenant_id"], "email": row["email"]}


def mark_email_verified(tid: str):
    with get_conn() as conn:
        conn.execute("UPDATE tenants SET email_verified=1 WHERE id=?", (tid,))


def can_send_reset_email(email: str) -> bool:
    """1小时内只能发一次重置邮件"""
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)
                    ).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        row = conn.execute("""
            SELECT id FROM email_tokens
            WHERE email=? AND type='reset' AND created_at > ? AND used=0
        """, (email, one_hour_ago)).fetchone()
    return row is None


def reset_tenant_password(tid: str, new_password: str = "reset123"):
    with get_conn() as conn:
        conn.execute("UPDATE tenants SET password_hash=? WHERE id=?",
                     (_hash(new_password), tid))


def is_trial_expired(tenant: dict) -> bool:
    if tenant.get("status") == "active":
        return False
    trial_ends = tenant.get("trial_ends", "")
    if not trial_ends:
        return False
    return trial_ends < _now()
