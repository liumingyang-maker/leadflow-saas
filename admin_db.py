"""
admin_db.py — 全局管理数据库
存储所有租户账号、订阅状态
"""
import sqlite3
import uuid
import hashlib
import hmac
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 数据目录：默认项目根目录；部署挂载持久卷时用环境变量 DATA_DIR（如 /data）
DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
ADMIN_DB_PATH = DATA_DIR / "admin.db"


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

            -- 独立站询盘插件：每租户一个专属 token，用于公开接收接口反查租户
            CREATE TABLE IF NOT EXISTS inbound_tokens (
                token       TEXT PRIMARY KEY,
                tenant_id   TEXT UNIQUE NOT NULL,
                created_at  TEXT
            );

            -- 自动跟进序列：客户发了开发信后，没回复就到点自动发下一封
            CREATE TABLE IF NOT EXISTS followups (
                id           TEXT PRIMARY KEY,
                tenant_id    TEXT NOT NULL,
                lead_id      TEXT NOT NULL,
                step         INTEGER DEFAULT 0,
                next_send_at TEXT,
                status       TEXT DEFAULT 'active',
                created_at   TEXT,
                updated_at   TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_followup_lead
                ON followups(tenant_id, lead_id);

            -- 邮件打开/点击追踪：每封通过 SMTP 通道发出的开发信一条记录
            CREATE TABLE IF NOT EXISTS email_tracking (
                tracking_id TEXT PRIMARY KEY,
                tenant_id   TEXT NOT NULL,
                lead_id     TEXT,
                subject     TEXT,
                sent_at     TEXT,
                opened_at   TEXT,
                open_count  INTEGER DEFAULT 0,
                last_open   TEXT,
                click_count INTEGER DEFAULT 0,
                last_click  TEXT
            );
        """)
        try:
            conn.execute("ALTER TABLE tenants ADD COLUMN email_verified INTEGER DEFAULT 0")
            conn.execute("UPDATE tenants SET email_verified=1")
        except Exception:
            pass
    _ensure_admin()


# ── 密码哈希（PBKDF2 + 随机盐）────────────────────────────

def _hash(password: str) -> str:
    """生成带盐的 PBKDF2-SHA256 哈希，格式：pbkdf2$<salt>$<hash>"""
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260000)
    return f"pbkdf2${salt.hex()}${key.hex()}"


def _verify(password: str, stored: str) -> bool:
    """验证密码，兼容旧版裸SHA-256格式（登录时自动升级）"""
    if stored.startswith("pbkdf2$"):
        _, salt_hex, key_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260000)
        return hmac.compare_digest(key.hex(), key_hex)
    # 旧格式 SHA-256 兼容
    return hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(), stored)


def _ensure_admin():
    """确保至少有一个管理员账号"""
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM admin_users LIMIT 1").fetchone()
        if not row:
            conn.execute(
                "INSERT INTO admin_users (id, email, password_hash, created_at) VALUES (?,?,?,?)",
                (str(uuid.uuid4()), "admin@leads.com", _hash("admin123"), _now())
            )


# ── 租户操作 ──────────────────────────────────────────────

def register_tenant(email: str, password: str) -> dict:
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM tenants WHERE email=?", (email,)
        ).fetchone()
        if existing:
            return {"ok": False, "error": "该邮箱已注册"}
        tid = uuid.uuid4().hex[:16]
        trial_ends = (datetime.now(timezone.utc) + timedelta(days=14)
                      ).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            INSERT INTO tenants
            (id, email, password_hash, status, created_at, trial_ends)
            VALUES (?,?,?,?,?,?)
        """, (tid, email, _hash(password), "trial", _now(), trial_ends))
    tenant_dir = DATA_DIR / "tenants" / tid
    tenant_dir.mkdir(parents=True, exist_ok=True)
    _init_tenant_config(tid)
    return {"ok": True, "tenant_id": tid}


def _init_tenant_config(tid: str):
    import json
    cfg_path = DATA_DIR / "tenants" / tid / "config.json"
    if not cfg_path.exists():
        default = {
            "company_name": "", "industry": "", "product_name": "",
            "product_desc": "", "hs_codes": [], "target_countries": [],
            "selected_regions": [], "excluded_countries": [],
            "search_keywords": [],
            "market_priority": {"tier1": [], "tier2": [], "tier3": []},
            "smtp_user": "", "smtp_pass": "",
            "importyeti_api_key": "", "serpapi_key": "",
            "hunter_api_key": "", "deepseek_api_key": "",
            "anthropic_api_key": "", "ai_enabled": False,
            "email_ai_mode": False, "email_from_name": "",
            "email_signature": "", "sender_name": "",
            "onboarding_step": 0,
        }
        cfg_path.write_text(json.dumps(default, ensure_ascii=False, indent=2),
                            encoding="utf-8")


def login_tenant(email: str, password: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE email=?", (email,)
        ).fetchone()
    if not row:
        return {"ok": False, "error": "邮箱或密码错误"}
    if not _verify(password, row["password_hash"]):
        return {"ok": False, "error": "邮箱或密码错误"}
    if row["status"] == "suspended":
        return {"ok": False, "error": "账号已被暂停，请联系客服"}
    # 旧密码格式自动升级
    if not row["password_hash"].startswith("pbkdf2$"):
        with get_conn() as conn:
            conn.execute("UPDATE tenants SET password_hash=? WHERE id=?",
                         (_hash(password), row["id"]))
    with get_conn() as conn:
        conn.execute("UPDATE tenants SET last_login=? WHERE id=?",
                     (_now(), row["id"]))
    return {"ok": True, "tenant": dict(row)}


def login_admin(email: str, password: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT password_hash FROM admin_users WHERE email=?", (email,)
        ).fetchone()
    if not row:
        return False
    return _verify(password, row["password_hash"])


def get_tenant_by_email(email: str):
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


def admin_create_tenant(email: str, password: str, company_name: str = "",
                        status: str = "active") -> dict:
    """管理员手动新建账号：直接激活、免邮箱验证（用于收款后开账号）。"""
    email = (email or "").strip().lower()
    if not email or not password:
        return {"ok": False, "error": "邮箱和密码必填"}
    if status not in ("active", "trial"):
        status = "active"
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM tenants WHERE email=?", (email,)).fetchone()
        if existing:
            return {"ok": False, "error": "该邮箱已存在"}
        tid = uuid.uuid4().hex[:16]
        trial_ends = (datetime.now(timezone.utc) + timedelta(days=14)
                      ).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""
            INSERT INTO tenants
            (id, email, password_hash, company_name, status,
             created_at, trial_ends, email_verified)
            VALUES (?,?,?,?,?,?,?,1)
        """, (tid, email, _hash(password), company_name.strip(),
              status, _now(), trial_ends))
    tenant_dir = DATA_DIR / "tenants" / tid
    tenant_dir.mkdir(parents=True, exist_ok=True)
    _init_tenant_config(tid)
    if company_name.strip():
        import json
        cfg_path = DATA_DIR / "tenants" / tid / "config.json"
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg["company_name"] = company_name.strip()
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        except Exception:
            pass
    return {"ok": True, "tenant_id": tid}


def delete_tenant(tid: str) -> dict:
    """彻底删除租户：账号记录 + 关联数据 + 数据目录（不可恢复）。"""
    import shutil
    with get_conn() as conn:
        conn.execute("DELETE FROM tenants WHERE id=?", (tid,))
        conn.execute("DELETE FROM email_tokens WHERE tenant_id=?", (tid,))
        conn.execute("DELETE FROM inbound_tokens WHERE tenant_id=?", (tid,))
        conn.execute("DELETE FROM followups WHERE tenant_id=?", (tid,))
        conn.execute("DELETE FROM email_tracking WHERE tenant_id=?", (tid,))
    tenant_dir = DATA_DIR / "tenants" / tid
    if tenant_dir.exists():
        shutil.rmtree(tenant_dir, ignore_errors=True)
    return {"ok": True}


def admin_update_tenant(tid: str, email: str = None,
                        company_name: str = None, password: str = None) -> dict:
    """管理员编辑账号：改邮箱 / 公司名 / 密码（任一可选，留空不动）。"""
    with get_conn() as conn:
        if email:
            email = email.strip().lower()
            dup = conn.execute(
                "SELECT id FROM tenants WHERE email=? AND id<>?",
                (email, tid)).fetchone()
            if dup:
                return {"ok": False, "error": "该邮箱已被其他账号使用"}
            conn.execute("UPDATE tenants SET email=? WHERE id=?", (email, tid))
        if company_name is not None:
            conn.execute("UPDATE tenants SET company_name=? WHERE id=?",
                         (company_name.strip(), tid))
        if password:
            conn.execute("UPDATE tenants SET password_hash=? WHERE id=?",
                         (_hash(password), tid))
    return {"ok": True}


# ── 邮箱验证 & 密码重置 ────────────────────────────────────

def create_email_token(tid: str, email: str, token_type: str) -> str:
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
    if not token or len(token) != 32:
        return {"ok": False, "error": "链接无效"}
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


# ── 独立站询盘 token ──────────────────────────────────────

def get_or_create_inbound_token(tid: str) -> str:
    """返回租户的独立站询盘专属 token，不存在则生成。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT token FROM inbound_tokens WHERE tenant_id=?", (tid,)
        ).fetchone()
        if row:
            return row["token"]
        token = "in_" + uuid.uuid4().hex
        conn.execute(
            "INSERT INTO inbound_tokens (token, tenant_id, created_at) VALUES (?,?,?)",
            (token, tid, _now()))
        return token


def regenerate_inbound_token(tid: str) -> str:
    """重置租户的询盘 token（旧的失效，用于泄露后更换）。"""
    token = "in_" + uuid.uuid4().hex
    with get_conn() as conn:
        conn.execute("DELETE FROM inbound_tokens WHERE tenant_id=?", (tid,))
        conn.execute(
            "INSERT INTO inbound_tokens (token, tenant_id, created_at) VALUES (?,?,?)",
            (token, tid, _now()))
    return token


def get_tid_by_inbound_token(token: str):
    """公开接收接口用：按 token 反查租户 id，找不到返回 None。"""
    if not token or not token.startswith("in_"):
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT tenant_id FROM inbound_tokens WHERE token=?", (token,)
        ).fetchone()
    return row["tenant_id"] if row else None


# ── 自动跟进序列 ──────────────────────────────────────────

def enroll_followup(tenant_id: str, lead_id: str, next_send_at: str) -> None:
    """客户发了首封开发信后登记进跟进序列；已存在则重置为 active 第0步。"""
    now = _now()
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO followups (id, tenant_id, lead_id, step, next_send_at,
                                   status, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(tenant_id, lead_id) DO UPDATE SET
                step=0, next_send_at=excluded.next_send_at,
                status='active', updated_at=excluded.updated_at
        """, (uuid.uuid4().hex, tenant_id, lead_id, 0, next_send_at,
              "active", now, now))


def get_due_followups(limit: int = 200) -> list:
    """返回所有到点该发的 active 跟进（next_send_at <= 现在）。"""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM followups
            WHERE status='active' AND next_send_at <= ?
            ORDER BY next_send_at ASC LIMIT ?
        """, (_now(), limit)).fetchall()
    return [dict(r) for r in rows]


def advance_followup(fid: str, step: int, next_send_at: str) -> None:
    with get_conn() as conn:
        conn.execute("""
            UPDATE followups SET step=?, next_send_at=?, updated_at=? WHERE id=?
        """, (step, next_send_at, _now(), fid))


def finish_followup(fid: str, status: str = "done") -> None:
    with get_conn() as conn:
        conn.execute("UPDATE followups SET status=?, updated_at=? WHERE id=?",
                     (status, _now(), fid))


def postpone_followup(fid: str, next_send_at: str) -> None:
    """发送失败时把下次发送时间往后推，稍后重试。"""
    with get_conn() as conn:
        conn.execute("UPDATE followups SET next_send_at=?, updated_at=? WHERE id=?",
                     (next_send_at, _now(), fid))


def cancel_followups_for_lead(tenant_id: str, lead_id: str) -> None:
    """客户已回复/已成交/已拒绝时，停止对其的自动跟进。"""
    with get_conn() as conn:
        conn.execute("""
            UPDATE followups SET status='stopped', updated_at=?
            WHERE tenant_id=? AND lead_id=? AND status='active'
        """, (_now(), tenant_id, lead_id))


# ── 邮件打开/点击追踪 ─────────────────────────────────────

def create_tracking(tenant_id: str, lead_id: str, subject: str) -> str:
    """发信时创建一条追踪记录，返回 tracking_id。"""
    tracking_id = "trk_" + uuid.uuid4().hex
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO email_tracking (tracking_id, tenant_id, lead_id, subject, sent_at)
            VALUES (?,?,?,?,?)
        """, (tracking_id, tenant_id, lead_id, subject, _now()))
    return tracking_id


def record_open(tracking_id: str) -> None:
    """像素被加载 = 邮件被打开。首次记 opened_at，之后累加。"""
    if not tracking_id or not tracking_id.startswith("trk_"):
        return
    with get_conn() as conn:
        conn.execute("""
            UPDATE email_tracking
            SET open_count = open_count + 1,
                last_open = ?,
                opened_at = COALESCE(opened_at, ?)
            WHERE tracking_id = ?
        """, (_now(), _now(), tracking_id))


def record_click(tracking_id: str) -> None:
    if not tracking_id or not tracking_id.startswith("trk_"):
        return
    with get_conn() as conn:
        conn.execute("""
            UPDATE email_tracking
            SET click_count = click_count + 1, last_click = ?,
                opened_at = COALESCE(opened_at, ?)
            WHERE tracking_id = ?
        """, (_now(), _now(), tracking_id))


def get_tracking_for_lead(tenant_id: str, lead_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM email_tracking
            WHERE tenant_id=? AND lead_id=? ORDER BY sent_at DESC
        """, (tenant_id, lead_id)).fetchall()
    return [dict(r) for r in rows]


def get_open_stats(tenant_id: str) -> dict:
    """租户维度的发信/打开/点击汇总，给看板用。"""
    with get_conn() as conn:
        row = conn.execute("""
            SELECT COUNT(*) sent,
                   SUM(CASE WHEN opened_at IS NOT NULL THEN 1 ELSE 0 END) opened,
                   SUM(CASE WHEN click_count > 0 THEN 1 ELSE 0 END) clicked
            FROM email_tracking WHERE tenant_id=?
        """, (tenant_id,)).fetchone()
    sent = row["sent"] or 0
    opened = row["opened"] or 0
    clicked = row["clicked"] or 0
    return {
        "sent": sent, "opened": opened, "clicked": clicked,
        "open_rate": round(opened / sent * 100, 1) if sent else 0.0,
        "click_rate": round(clicked / sent * 100, 1) if sent else 0.0,
    }


# ── 清理过期 token（定期调用）─────────────────────────────

def cleanup_expired_tokens():
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM email_tokens WHERE expires_at < ? OR used=1",
            (_now(),)
        )
