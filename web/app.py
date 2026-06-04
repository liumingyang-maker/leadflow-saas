"""
web/app.py — SaaS 主应用
"""
import sys
import os
import json
import time
import threading
import uuid as _uuid
import csv as _csv
from pathlib import Path
from functools import wraps
from collections import defaultdict
from datetime import datetime

from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, send_file, flash)
from flask_cors import CORS

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "core"))

import admin_db
import tenant_ctx
from mailer import send_verification_email, send_reset_email


# ─────────────────────────────────────────────────────────
# 应用初始化
# ─────────────────────────────────────────────────────────

app = Flask(__name__, template_folder="templates")

# secret_key 持久化——重启不丢 session
_SK_FILE = BASE / "secret_key.bin"
if _SK_FILE.exists():
    app.secret_key = _SK_FILE.read_bytes()
else:
    _sk = os.urandom(32)
    _SK_FILE.write_bytes(_sk)
    app.secret_key = _sk

# CORS 只允许同源，不开放给所有域
CORS(app, origins=["http://127.0.0.1:5001", "http://localhost:5001"])

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,   # JS 无法读取 cookie
    SESSION_COOKIE_SAMESITE="Lax",  # 防 CSRF
    PERMANENT_SESSION_LIFETIME=86400 * 7,  # 7天登录有效期
)


# ─────────────────────────────────────────────────────────
# 安全响应头
# ─────────────────────────────────────────────────────────

@app.after_request
def add_security_headers(resp):
    resp.headers["X-Frame-Options"]        = "SAMEORIGIN"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-XSS-Protection"]       = "1; mode=block"
    resp.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"]     = "geolocation=(), microphone=()"
    return resp


# ─────────────────────────────────────────────────────────
# 频率限制（防暴力破解）
# ─────────────────────────────────────────────────────────

class _RateLimiter:
    def __init__(self):
        self._lock  = threading.Lock()
        self._hits  = defaultdict(list)

    def check(self, key: str, max_hits: int, window: int) -> bool:
        """返回 True 表示允许，False 表示超限"""
        now = time.time()
        with self._lock:
            self._hits[key] = [t for t in self._hits[key] if now - t < window]
            if len(self._hits[key]) >= max_hits:
                return False
            self._hits[key].append(now)
            return True

_rl = _RateLimiter()


def _client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()


def rate_limit(max_hits: int, window: int, scope: str = ""):
    """路由装饰器：超限返回 429"""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            key = f"{scope or f.__name__}:{_client_ip()}"
            if not _rl.check(key, max_hits, window):
                return render_template("auth/login.html",
                    error="操作过于频繁，请稍后再试"), 429
            return f(*args, **kwargs)
        return wrapped
    return decorator


# ─────────────────────────────────────────────────────────
# 工具装饰器
# ─────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "tenant_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def onboarding_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "tenant_id" not in session:
            return redirect(url_for("login"))
        cfg = tenant_ctx.load_config(session["tenant_id"])
        if not cfg.get("onboarding_step", 0) >= 5:
            return redirect(url_for("onboarding_step",
                            step=max(1, cfg.get("onboarding_step", 0) + 1)))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated


def current_cfg():
    return tenant_ctx.load_config(session.get("tenant_id", ""))


def current_tid():
    return session.get("tenant_id", "")


# ─────────────────────────────────────────────────────────
# 首页 / 登录 / 注册
# ─────────────────────────────────────────────────────────

@app.route("/")
def index():
    if "tenant_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
@rate_limit(max_hits=10, window=60, scope="login")
def login():
    error = ""
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()[:120]
        password = request.form.get("password", "")[:128]
        if not email or not password:
            error = "请填写邮箱和密码"
        else:
            result = admin_db.login_tenant(email, password)
            if result["ok"]:
                t = result["tenant"]
                if not t.get("email_verified", 0):
                    error = "邮箱尚未验证，请查收注册邮件"
                elif admin_db.is_trial_expired(t):
                    error = "试用期已过，请联系客服开通正式版"
                else:
                    session.permanent = True
                    session["tenant_id"]    = t["id"]
                    session["tenant_email"] = t["email"]
                    session["company_name"] = t.get("company_name", "")
                    cfg = tenant_ctx.load_config(t["id"])
                    if cfg.get("onboarding_step", 0) < 5:
                        return redirect(url_for("onboarding_step",
                                        step=max(1, cfg.get("onboarding_step", 0) + 1)))
                    return redirect(url_for("dashboard"))
            else:
                error = result["error"]
    return render_template("auth/login.html", error=error)


@app.route("/register", methods=["GET", "POST"])
@rate_limit(max_hits=5, window=300, scope="register")
def register():
    error = ""
    if request.method == "POST":
        email     = request.form.get("email", "").strip().lower()[:120]
        password  = request.form.get("password", "")[:128]
        password2 = request.form.get("password2", "")[:128]
        if not email or not password:
            error = "请填写邮箱和密码"
        elif "@" not in email or "." not in email.split("@")[-1]:
            error = "请填写有效的邮箱地址"
        elif password != password2:
            error = "两次密码不一致"
        elif len(password) < 6:
            error = "密码至少6位"
        else:
            result = admin_db.register_tenant(email, password)
            if result["ok"]:
                tid   = result["tenant_id"]
                token = admin_db.create_email_token(tid, email, "verify")
                send_verification_email(email, token)
                return redirect(url_for("verify_pending", email=email))
            else:
                error = result["error"]
    return render_template("auth/register.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/verify-pending")
def verify_pending():
    email = request.args.get("email", "")[:120]
    return render_template("auth/verify_pending.html", email=email)


@app.route("/verify-email/<token>")
def verify_email(token):
    result = admin_db.verify_email_token(token, "verify")
    if not result["ok"]:
        return render_template("auth/verify_pending.html",
                               email="", error=result["error"])
    admin_db.mark_email_verified(result["tenant_id"])
    session["tenant_id"]    = result["tenant_id"]
    session["tenant_email"] = result["email"]
    return redirect(url_for("onboarding_step", step=1))


@app.route("/resend-verify", methods=["POST"])
@rate_limit(max_hits=3, window=300, scope="resend")
def resend_verify():
    email = request.form.get("email", "").strip().lower()[:120]
    if email:
        row = admin_db.get_tenant_by_email(email)
        if row and not row.get("email_verified", 0):
            token = admin_db.create_email_token(row["id"], email, "verify")
            send_verification_email(email, token)
    return render_template("auth/verify_pending.html", email=email, resent=True)


@app.route("/forgot-password", methods=["GET", "POST"])
@rate_limit(max_hits=5, window=300, scope="forgot")
def forgot_password():
    msg = ""
    error = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:120]
        row   = admin_db.get_tenant_by_email(email)
        if not row:
            error = "该邮箱未注册"
        elif not admin_db.can_send_reset_email(email):
            error = "1小时内已发送过重置邮件，请检查邮箱或稍后再试"
        else:
            token = admin_db.create_email_token(row["id"], email, "reset")
            send_reset_email(email, token)
            msg = "重置邮件已发送，请查收（30分钟内有效）"
    return render_template("auth/forgot_password.html", msg=msg, error=error)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    error  = ""
    result = admin_db.verify_email_token(token, "reset")
    if not result["ok"]:
        return render_template("auth/reset_password.html",
                               token=token, expired=True, error=result["error"])
    if request.method == "POST":
        pw  = request.form.get("password", "")[:128]
        pw2 = request.form.get("password2", "")[:128]
        if len(pw) < 6:
            error = "密码至少6位"
        elif pw != pw2:
            error = "两次密码不一致"
        else:
            admin_db.reset_tenant_password(result["tenant_id"], pw)
            return redirect(url_for("login") + "?reset=1")
    return render_template("auth/reset_password.html",
                           token=token, expired=False,
                           error=error, email=result["email"])


# ─────────────────────────────────────────────────────────
# 入驻向导（5步）
# ─────────────────────────────────────────────────────────

@app.route("/onboarding/<int:step>", methods=["GET", "POST"])
@login_required
def onboarding_step(step):
    if step not in range(1, 6):
        return redirect(url_for("dashboard"))
    tid = current_tid()
    cfg = tenant_ctx.load_config(tid)

    if request.method == "POST":
        if step == 1:
            cfg["company_name"] = request.form.get("company_name", "").strip()[:100]
            cfg["industry"]     = request.form.get("industry", "")
            cfg["product_name"] = request.form.get("product_name", "").strip()[:100]
            cfg["product_desc"] = request.form.get("product_desc", "").strip()[:500]
            session["company_name"] = cfg["company_name"]
            admin_db.update_tenant(tid, company_name=cfg["company_name"],
                                   industry=cfg["industry"])

        elif step == 2:
            hs_raw = request.form.get("hs_codes", "")
            cfg["hs_codes"] = [h.strip() for h in
                               hs_raw.replace("，", ",").split(",") if h.strip()][:20]
            kw_raw = request.form.get("search_keywords", "")
            cfg["search_keywords"] = [k.strip() for k in
                                      kw_raw.split("\n") if k.strip()][:30]

        elif step == 3:
            selected_regions   = json.loads(request.form.get("selected_regions", "[]"))
            excluded_countries = json.loads(request.form.get("excluded_countries", "[]"))
            excluded_set = set(excluded_countries)
            final = [c for r in selected_regions
                       for c in tenant_ctx.REGIONS.get(r, [])
                       if c not in excluded_set]
            cfg["selected_regions"]   = selected_regions
            cfg["excluded_countries"] = excluded_countries
            cfg["target_countries"]   = final
            cfg["market_priority"]    = {
                "tier1": final[:3], "tier2": final[3:8], "tier3": final[8:],
            }

        elif step == 4:
            for k in ("importyeti_api_key", "serpapi_key", "hunter_api_key",
                      "deepseek_api_key", "anthropic_api_key"):
                v = request.form.get(k, "").strip()[:200]
                if v:
                    cfg[k] = v

        elif step == 5:
            cfg["sender_name"]      = request.form.get("sender_name", "").strip()[:80]
            cfg["email_from_name"]  = request.form.get("email_from_name", "").strip()[:80]
            cfg["smtp_user"]        = request.form.get("smtp_user", "").strip()[:120]
            cfg["smtp_pass"]        = request.form.get("smtp_pass", "").strip()[:120]
            cfg["email_signature"]  = request.form.get("email_signature", "").strip()[:500]

        cfg["onboarding_step"] = step
        tenant_ctx.save_config(tid, cfg)

        if step < 5:
            return redirect(url_for("onboarding_step", step=step + 1))
        admin_db.update_tenant(tid, onboarding_done=1)
        _init_tenant_db(tid)
        return redirect(url_for("dashboard"))

    return render_template(f"onboarding/step{step}.html", step=step, cfg=cfg,
                           industries=tenant_ctx.INDUSTRY_OPTIONS,
                           regions=tenant_ctx.REGIONS)


def _init_tenant_db(tid: str):
    from database import Database
    d = Database(db_path=tenant_ctx.get_db_path(tid))
    d.init()


# ─────────────────────────────────────────────────────────
# 主看板
# ─────────────────────────────────────────────────────────

@app.route("/dashboard")
@onboarding_required
def dashboard():
    tid = current_tid()
    db  = _get_db(tid)
    leads, total = db.search_leads(limit=50, offset=0)
    return render_template("app/index.html", cfg=current_cfg(),
                           leads=leads, total=total,
                           stats=db.get_stats(),
                           countries=db.get_all_countries(),
                           page=1, total_pages=1, filters={})


@app.route("/leads")
@onboarding_required
def leads_list():
    tid      = current_tid()
    db       = _get_db(tid)
    grade    = request.args.get("grade")
    status   = request.args.get("status")
    country  = request.args.get("country")
    keyword  = request.args.get("q", "")[:100]
    page     = max(1, int(request.args.get("page", 1)))
    per_page = 50
    offset   = (page - 1) * per_page
    leads, total = db.search_leads(keyword=keyword, country=country,
                                   grade=grade, status=status,
                                   limit=per_page, offset=offset)
    total_pages = max(1, (total + per_page - 1) // per_page)
    return render_template("app/index.html", cfg=current_cfg(),
                           leads=leads, total=total,
                           stats=db.get_stats(),
                           countries=db.get_all_countries(),
                           page=page, total_pages=total_pages,
                           filters={"grade": grade, "status": status,
                                    "country": country, "q": keyword})


@app.route("/lead/<lead_id>")
@onboarding_required
def lead_detail(lead_id):
    db   = _get_db(current_tid())
    lead = db.get_lead(lead_id)
    if not lead:
        return "Not found", 404
    return render_template("app/detail.html", cfg=current_cfg(),
                           lead=lead, history=db.get_outreach_history(lead_id))


@app.route("/lead/<lead_id>/update", methods=["POST"])
@onboarding_required
def update_lead(lead_id):
    db   = _get_db(current_tid())
    data = request.get_json(silent=True) or {}
    allowed = {"status", "notes", "email", "phone", "contact_name",
               "contact_title", "linkedin_url", "website", "grade"}
    updates = {k: str(v)[:500] for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({"ok": False}), 400
    return jsonify({"ok": db.update_lead(lead_id, updates)})


@app.route("/lead/<lead_id>/delete", methods=["POST"])
@onboarding_required
def delete_lead(lead_id):
    return jsonify({"ok": _get_db(current_tid()).delete_lead(lead_id)})


@app.route("/export")
@onboarding_required
def export_csv():
    tid      = current_tid()
    db       = _get_db(tid)
    filepath = tenant_ctx.tenant_dir(tid) / "export.csv"
    db.export_to_csv(str(filepath))
    return send_file(str(filepath), as_attachment=True,
                     download_name="leads_export.csv")


@app.route("/stats")
@onboarding_required
def stats_page():
    db = _get_db(current_tid())
    return render_template("app/stats.html", cfg=current_cfg(),
                           stats=db.get_stats(),
                           history=db.get_collection_history(limit=10))


# ─────────────────────────────────────────────────────────
# 任务触发（采集/评分）
# ─────────────────────────────────────────────────────────

_task_lock   = threading.Lock()
_task_status = {}
_TASK_TTL    = 3600  # 1小时后清理旧任务


def _cleanup_tasks():
    cutoff = time.time() - _TASK_TTL
    with _task_lock:
        old = [k for k, v in _task_status.items()
               if v.get("_ts", 0) < cutoff]
        for k in old:
            del _task_status[k]


@app.route("/run/<step>", methods=["POST"])
@onboarding_required
def run_step(step):
    if step not in {"collect", "score", "enrich", "all"}:
        return jsonify({"ok": False, "error": "未知步骤"}), 400
    _cleanup_tasks()
    tid     = current_tid()
    cfg     = current_cfg()
    task_id = f"{step}_{datetime.now().strftime('%H%M%S')}_{_uuid.uuid4().hex[:4]}"

    with _task_lock:
        _task_status[task_id] = {"status": "running", "log": [], "_ts": time.time()}

    def run_bg():
        logs = []
        try:
            db       = _get_db(tid)
            countries = cfg.get("target_countries", [])[:20]

            if step in ("collect", "all"):
                from module2_cleaner import DataCleaner

                # ImportYeti
                from module1_collectors.importyeti import ImportYetiCollector
                col = ImportYetiCollector()
                col.api_key = cfg.get("importyeti_api_key", "")
                col.mode    = "api" if col.api_key else "scrape"
                raw = col.fetch_all(mock=not col.api_key)
                if raw:
                    s = DataCleaner().run(raw, source="importyeti",
                                         db_path=tenant_ctx.get_db_path(tid))
                    logs.append(f"ImportYeti: 新增 {s.get('db_new',0)} 条")

                # Serper + DeepSeek
                serper_key   = cfg.get("serpapi_key", "")
                deepseek_key = cfg.get("deepseek_api_key", "")
                if serper_key or deepseek_key:
                    from module1_collectors.google_search import SerperDeepSeekCollector
                    gc = SerperDeepSeekCollector()
                    gc.serper_key      = serper_key
                    gc.deepseek_key    = deepseek_key
                    gc.product_name    = cfg.get("product_name", "")
                    gc.search_keywords = cfg.get("search_keywords", [])
                    g_raw = gc.fetch_all(countries=countries, mock=not serper_key)
                    if g_raw:
                        gs = DataCleaner().run(g_raw, source="google",
                                              db_path=tenant_ctx.get_db_path(tid))
                        logs.append(f"搜索采集: 新增 {gs.get('db_new',0)} 条")

            if step in ("score", "all"):
                from module3_scorer import LeadScorer
                s = LeadScorer(db_path=tenant_ctx.get_db_path(tid)).run(use_ai=False)
                logs.append(f"评分: A={s.get('grade_A',0)} B={s.get('grade_B',0)}")

            with _task_lock:
                _task_status[task_id] = {"status": "done", "log": logs,
                                         "_ts": time.time()}
        except Exception as e:
            with _task_lock:
                _task_status[task_id] = {"status": "error", "log": [str(e)],
                                         "_ts": time.time()}

    threading.Thread(target=run_bg, daemon=True).start()
    return jsonify({"ok": True, "task_id": task_id})


@app.route("/task/<task_id>")
@login_required
def task_status(task_id):
    with _task_lock:
        data = dict(_task_status.get(task_id, {"status": "not_found"}))
    data.pop("_ts", None)
    return jsonify(data)


# ─────────────────────────────────────────────────────────
# 系统设置
# ─────────────────────────────────────────────────────────

@app.route("/settings", methods=["GET", "POST"])
@onboarding_required
def settings():
    tid   = current_tid()
    cfg   = current_cfg()
    saved = False
    if request.method == "POST":
        for k in ("importyeti_api_key", "serpapi_key", "hunter_api_key",
                  "deepseek_api_key", "anthropic_api_key",
                  "smtp_user", "smtp_pass", "sender_name",
                  "email_from_name", "email_signature"):
            v = request.form.get(k, "").strip()[:200]
            if v:
                cfg[k] = v
        cfg["ai_enabled"]    = request.form.get("AI_ENABLED") == "on"
        cfg["email_ai_mode"] = request.form.get("EMAIL_AI_MODE") == "on"
        tenant_ctx.save_config(tid, cfg)
        session["company_name"] = cfg.get("company_name", "")
        saved = True
    return render_template("app/settings.html", cfg=cfg, saved=saved,
                           regions=tenant_ctx.REGIONS)


# ─────────────────────────────────────────────────────────
# 邮件模板
# ─────────────────────────────────────────────────────────

@app.route("/email-templates", methods=["GET", "POST"])
@onboarding_required
def email_templates():
    tid      = current_tid()
    cfg      = current_cfg()
    tpl_path = tenant_ctx.get_email_templates_path(tid)
    saved    = False
    templates = (json.loads(tpl_path.read_text(encoding="utf-8"))
                 if tpl_path.exists() else _default_email_templates(cfg))
    if request.method == "POST":
        for key in templates:
            s = request.form.get(f"subject_{key}", "").strip()[:200]
            b = request.form.get(f"body_{key}", "").strip()[:2000]
            if s: templates[key]["subject"] = s
            if b: templates[key]["body"]    = b
        tpl_path.write_text(json.dumps(templates, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        saved = True
    return render_template("app/email_tpl.html", cfg=cfg,
                           templates=templates, saved=saved)


@app.route("/email-templates/reset")
@onboarding_required
def email_templates_reset():
    tid      = current_tid()
    cfg      = current_cfg()
    tpl_path = tenant_ctx.get_email_templates_path(tid)
    tpl_path.write_text(
        json.dumps(_default_email_templates(cfg), ensure_ascii=False, indent=2),
        encoding="utf-8")
    return redirect(url_for("email_templates"))


def _default_email_templates(cfg: dict) -> dict:
    company = cfg.get("company_name", "Our Company")
    product = cfg.get("product_name", "our products")
    return {
        "first_contact": {
            "display_name": "首封开发信", "desc": "第一次联系买家",
            "subject": f"{product} Supply — {{company_name}}",
            "body": (f"Hi {{contact_name}},\n\nI came across {{company_name}} "
                     f"and noticed your business in {{country}}.\n\n"
                     f"We are {company}, a manufacturer specializing in {product}.\n\n"
                     f"Would you be open to a quick call this week?\n\n"
                     f"Best regards,\n{{sender_name}}\n{company}"),
        },
        "follow_up": {
            "display_name": "7天跟进", "desc": "首封发出7天无回复",
            "subject": f"Re: {product} — Following Up",
            "body": (f"Hi {{contact_name}},\n\nJust following up on my previous email "
                     f"about {product}.\n\nWould you be interested in receiving our "
                     f"latest price list?\n\nBest,\n{{sender_name}}\n{company}"),
        },
        "holiday_greeting": {
            "display_name": "节日问候", "desc": "维系客户关系",
            "subject": f"Happy New Year from {company}!",
            "body": (f"Dear {{contact_name}},\n\nWishing you a Happy New Year from "
                     f"all of us at {company}!\n\nLooking forward to working with "
                     f"you in the new year.\n\nWarm regards,\n{{sender_name}}"),
        },
    }


# ─────────────────────────────────────────────────────────
# 展会数据导入
# ─────────────────────────────────────────────────────────

@app.route("/import")
@onboarding_required
def import_page():
    return render_template("app/import.html", cfg=current_cfg())


@app.route("/import/upload", methods=["POST"])
@onboarding_required
def import_upload():
    tid    = current_tid()
    cfg    = current_cfg()
    f      = request.files.get("file")
    source = request.form.get("source", "展会").strip()[:50] or "展会"
    if not f or not f.filename:
        return redirect(url_for("import_page"))
    ext = Path(f.filename).suffix.lower()
    if ext not in (".xlsx", ".xls", ".csv"):
        return redirect(url_for("import_page"))
    tmp_id  = _uuid.uuid4().hex
    tmp_dir = tenant_ctx.tenant_dir(tid) / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = tmp_dir / f"{tmp_id}{ext}"
    f.save(str(tmp_path))
    try:
        import pandas as pd
        df = (pd.read_excel(tmp_path, dtype=str) if ext in (".xlsx", ".xls")
              else pd.read_csv(tmp_path, dtype=str, encoding_errors="replace"))
        df = df.fillna("")
        columns = list(df.columns)
        rows    = df.values.tolist()
    except Exception:
        return redirect(url_for("import_page"))
    auto_map = _auto_map_columns(columns)
    csv_path = tmp_dir / f"{tmp_id}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fp:
        w = _csv.writer(fp)
        w.writerow(columns)
        w.writerows(rows)
    tmp_path.unlink(missing_ok=True)
    return render_template("app/import.html", cfg=cfg, preview=True,
                           tmpfile=tmp_id, source=source, columns=columns,
                           auto_map=auto_map, preview_rows=rows[:5],
                           total_rows=len(rows))


@app.route("/import/confirm", methods=["POST"])
@onboarding_required
def import_confirm():
    tid      = current_tid()
    tmp_id   = request.form.get("tmpfile", "")
    source   = request.form.get("source", "展会")[:50]
    csv_path = tenant_ctx.tenant_dir(tid) / "tmp" / f"{tmp_id}.csv"
    if not csv_path.exists():
        return redirect(url_for("import_page"))
    with open(csv_path, newline="", encoding="utf-8") as fp:
        reader  = _csv.reader(fp)
        columns = next(reader)
        rows    = list(reader)
    mapping = {i: request.form.get(f"map_{i}", "")
               for i in range(len(columns))}
    raw_leads = []
    for row in rows:
        lead = {"source": source}
        for idx, field in mapping.items():
            if field and idx < len(row):
                lead[field] = row[idx].strip()[:300]
        if lead.get("company_name"):
            raw_leads.append(lead)
    csv_path.unlink(missing_ok=True)
    if not raw_leads:
        return render_template("app/import.html", cfg=current_cfg(),
                               result={"total": len(rows), "db_new": 0,
                                       "skipped": 0, "invalid": len(rows)})
    from module2_cleaner import DataCleaner
    stats = DataCleaner().run(raw_leads, source=source,
                              db_path=tenant_ctx.get_db_path(tid))
    return render_template("app/import.html", cfg=current_cfg(),
                           result={"total": len(rows),
                                   "db_new":  stats.get("db_new", 0),
                                   "skipped": stats.get("db_skipped", 0),
                                   "invalid": stats.get("invalid", 0)})


def _auto_map_columns(columns):
    keywords = {
        "company_name":  ["company","公司","企业","name","firm"],
        "country":       ["country","国家","nation"],
        "email":         ["email","邮件","邮箱","mail"],
        "phone":         ["phone","电话","tel","mobile"],
        "contact_name":  ["contact","联系人","person"],
        "contact_title": ["title","职位","position"],
        "website":       ["website","网站","web","url"],
    }
    result = [""] * len(columns)
    used   = set()
    for i, col in enumerate(columns):
        cl = col.lower()
        for field, kws in keywords.items():
            if field in used:
                continue
            if any(k in cl for k in kws):
                result[i] = field
                used.add(field)
                break
    return result


# ─────────────────────────────────────────────────────────
# 管理员后台
# ─────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
@rate_limit(max_hits=5, window=60, scope="admin_login")
def admin_login():
    error = ""
    if request.method == "POST":
        email    = request.form.get("email", "")[:120]
        password = request.form.get("password", "")[:128]
        if admin_db.login_admin(email, password):
            session["is_admin"]    = True
            session["admin_email"] = email
            return redirect(url_for("admin_panel"))
        error = "账号或密码错误"
    return render_template("admin/login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_panel():
    return render_template("admin/panel.html", tenants=admin_db.all_tenants())


@app.route("/admin/tenant/<tid>/activate", methods=["POST"])
@admin_required
def admin_activate(tid):
    admin_db.update_tenant(tid, status="active")
    return redirect(url_for("admin_panel"))


@app.route("/admin/tenant/<tid>/suspend", methods=["POST"])
@admin_required
def admin_suspend(tid):
    admin_db.update_tenant(tid, status="suspended")
    return redirect(url_for("admin_panel"))


@app.route("/admin/tenant/<tid>/trial", methods=["POST"])
@admin_required
def admin_set_trial(tid):
    admin_db.update_tenant(tid, status="trial")
    return redirect(url_for("admin_panel"))


@app.route("/admin/tenant/<tid>/reset_password", methods=["POST"])
@admin_required
def admin_reset_password(tid):
    admin_db.reset_tenant_password(tid)
    flash("密码已重置为 reset123，请告知客户登录后修改密码")
    return redirect(url_for("admin_panel"))


@app.route("/admin/tenant/<tid>/note", methods=["POST"])
@admin_required
def admin_note(tid):
    note = request.form.get("note", "")[:200]
    admin_db.update_tenant(tid, note=note)
    return redirect(url_for("admin_panel"))


# ─────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────

_db_cache      = {}
_db_cache_lock = threading.Lock()


def _get_db(tid: str):
    """线程安全地获取租户数据库实例"""
    with _db_cache_lock:
        if tid not in _db_cache:
            from database import Database
            d = Database(db_path=tenant_ctx.get_db_path(tid))
            d.init()
            _db_cache[tid] = d
        return _db_cache[tid]


# ─────────────────────────────────────────────────────────
# 启动
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    admin_db.init()
    app.run(host="127.0.0.1", port=5001, debug=True, use_reloader=False)
