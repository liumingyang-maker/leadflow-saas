"""
web/app.py — SaaS 主应用
"""
import sys
import os
import re
import json
import time
import threading
import uuid as _uuid
import csv as _csv
from pathlib import Path
from functools import wraps
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, send_file, flash, Response)
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

# secret_key：优先环境变量 SECRET_KEY（生产固定，重启/重新部署不掉登录）；
# 否则用数据目录里的文件（挂持久卷时也能留住）
_DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE)))
_sk_env = os.environ.get("SECRET_KEY")
if _sk_env:
    app.secret_key = _sk_env.encode("utf-8")
else:
    _SK_FILE = _DATA_DIR / "secret_key.bin"
    if _SK_FILE.exists():
        app.secret_key = _SK_FILE.read_bytes()
    else:
        _sk = os.urandom(32)
        try:
            _SK_FILE.parent.mkdir(parents=True, exist_ok=True)
            _SK_FILE.write_bytes(_sk)
        except Exception:
            pass
        app.secret_key = _sk

# 持久登录："60天免密登录"勾选时 session.permanent=True，按此存活期保持登录
app.permanent_session_lifetime = timedelta(days=60)

# CORS：允许本机 + 生产域名（SITE_URL）。独立站询盘的公开接口另有 _cors 放行 *
_cors_origins = ["http://127.0.0.1:5001", "http://localhost:5001"]
_site_origin = os.environ.get("SITE_URL", "").rstrip("/")
if _site_origin and _site_origin not in _cors_origins:
    _cors_origins.append(_site_origin)
CORS(app, origins=_cors_origins)

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
    """运行用配置：注入平台代付的 DeepSeek/Serper 主 key（客户没填自己的时）。
    采集/雷达/AI 都经它拿 key → 零配置即用。注意：注入的是副本、不回存；
    设置页/额度查询要用 tenant_ctx.load_config(raw) 而非这个，避免把平台 key 写进 config.json。"""
    from api_platform import runtime_cfg
    return runtime_cfg(session.get("tenant_id", ""))


def current_tid():
    return session.get("tenant_id", "")


def _wa_api_ready(cfg: dict) -> bool:
    """该租户是否已配好 WhatsApp 官方 API（A 方案）。"""
    try:
        from whatsapp_sender import WhatsAppSender
        return WhatsAppSender(cfg).is_configured()
    except Exception:
        return False


@app.context_processor
def inject_account():
    """给所有模板注入账户信息（顶栏/右栏用）：公司名、套餐状态、试用剩余天数。"""
    tid = session.get("tenant_id")
    if not tid:
        return {"acct": {"company": "", "status": "", "trial_days_left": None, "email": ""}}
    try:
        t = admin_db.get_tenant(tid) or {}
        days_left = None
        te = t.get("trial_ends")
        if te:
            try:
                end = datetime.strptime(te, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                days_left = max(0, (end - datetime.now(timezone.utc)).days)
            except Exception:
                pass
        return {"acct": {
            "company": t.get("company_name") or session.get("company_name", ""),
            "status": t.get("status", "trial"),
            "trial_days_left": days_left,
            "email": t.get("email", ""),
        }}
    except Exception:
        return {"acct": {"company": session.get("company_name", ""),
                         "status": "", "trial_days_left": None, "email": ""}}


# ─────────────────────────────────────────────────────────
# 首页 / 登录 / 注册
# ─────────────────────────────────────────────────────────

@app.route("/")
def index():
    if "tenant_id" in session:
        return redirect(url_for("workbench"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
@rate_limit(max_hits=10, window=60, scope="login")
def login():
    error = ""
    email = ""
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
                    # 勾选"60天免密登录"才持久化(60天)，否则浏览器关闭即失效
                    session.permanent = request.form.get("remember_login") == "on"
                    session["tenant_id"]    = t["id"]
                    session["tenant_email"] = t["email"]
                    session["company_name"] = t.get("company_name", "")
                    cfg = tenant_ctx.load_config(t["id"])
                    if cfg.get("onboarding_step", 0) < 5:
                        return redirect(url_for("onboarding_step",
                                        step=max(1, cfg.get("onboarding_step", 0) + 1)))
                    return redirect(url_for("workbench"))
            else:
                error = result["error"]
    return render_template("auth/login.html", error=error, email=email)


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
        return redirect(url_for("workbench"))
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
                      "deepseek_api_key", "anthropic_api_key", "apollo_api_key"):
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
        return redirect(url_for("workbench"))

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

@app.route("/workbench")
@onboarding_required
def workbench():
    tid = current_tid()
    db  = _get_db(tid)
    try:
        email_stats = admin_db.get_open_stats(tid)
    except Exception:
        email_stats = {"sent": 0, "opened": 0, "open_rate": 0.0}
    recent_leads, _ = db.search_leads(limit=6, offset=0)
    try:
        _, review_count = db.search_leads(status="review", limit=1, offset=0)
    except Exception:
        review_count = 0
    try:
        from pipeline_config import pipeline_summary
        pipeline = pipeline_summary()
    except Exception:
        pipeline = []
    return render_template("app/workbench.html", cfg=current_cfg(),
                           stats=db.get_stats(),
                           email_stats=email_stats,
                           recent_leads=recent_leads,
                           review_count=review_count,
                           pipeline=pipeline,
                           history=db.get_collection_history(limit=5))


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
    source   = request.args.get("source")
    competitor = request.args.get("competitor", "")[:120]
    keyword  = request.args.get("q", "")[:100]
    page     = max(1, int(request.args.get("page", 1)))
    per_page = 50
    offset   = (page - 1) * per_page
    # 点竞品卡的"X 经销商"跳来：只看该竞品挖出的渠道商（线索 notes 里有"竞品 {host}"）
    notes_like = f"竞品 {competitor}" if competitor else None
    leads, total = db.search_leads(keyword=keyword, country=country,
                                   grade=grade, status=status, source=source,
                                   notes_like=notes_like,
                                   limit=per_page, offset=offset)
    total_pages = max(1, (total + per_page - 1) // per_page)
    return render_template("app/index.html", cfg=current_cfg(),
                           leads=leads, total=total,
                           stats=db.get_stats(),
                           countries=db.get_all_countries(),
                           page=page, total_pages=total_pages,
                           filters={"grade": grade, "status": status,
                                    "country": country, "q": keyword,
                                    "source": source, "competitor": competitor})


@app.route("/lead/<lead_id>")
@onboarding_required
def lead_detail(lead_id):
    db   = _get_db(current_tid())
    lead = db.get_lead(lead_id)
    if not lead:
        return "Not found", 404
    cfg = current_cfg()
    try:
        email_tracking = admin_db.get_tracking_for_lead(current_tid(), lead_id)
    except Exception:
        email_tracking = []
    return render_template("app/detail.html", cfg=cfg,
                           lead=lead, history=db.get_outreach_history(lead_id),
                           email_tracking=email_tracking,
                           templates=_render_templates_for_lead(current_tid(), cfg, lead),
                           mail_channel_ready=bool(
                               (cfg.get("mail_channel", "smtp") == "esp"
                                and cfg.get("esp_api_key") and cfg.get("esp_from_email"))
                               or (cfg.get("mail_channel", "smtp") == "smtp"
                                   and cfg.get("smtp_host") and cfg.get("smtp_user"))),
                           wa_api_ready=_wa_api_ready(cfg))


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
    ok = db.update_lead(lead_id, updates)
    # 客户已回复/成交/拒绝 → 停止对其的自动跟进
    if updates.get("status") in ("replied", "converted", "rejected"):
        try:
            admin_db.cancel_followups_for_lead(current_tid(), lead_id)
        except Exception as e:
            print(f"[followup] 取消失败: {e}")
    return jsonify({"ok": ok})


@app.route("/lead/<lead_id>/delete", methods=["POST"])
@onboarding_required
def delete_lead(lead_id):
    return jsonify({"ok": _get_db(current_tid()).delete_lead(lead_id)})


# ── 一键找邮箱（theHarvester + Photon）──────────────────────────────
@app.route("/lead/<lead_id>/enrich", methods=["POST"])
@onboarding_required
def enrich_lead_contacts(lead_id):
    tid  = current_tid()
    cfg  = current_cfg()
    db   = _get_db(tid)
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"ok": False, "error": "客户不存在"}), 404
    try:
        from module1_collectors.email_enricher import EmailEnricher
        en = EmailEnricher()
        en.serper_key = cfg.get("serpapi_key", "")
        en.hunter_key = cfg.get("hunter_api_key", "")
        result = en.enrich(website=lead.get("website", ""),
                           company_name=lead.get("company_name", ""))
        # 找到最佳邮箱且该客户原本没邮箱 → 自动写回数据库
        saved = False
        if result.get("best_email") and not lead.get("email"):
            fields = {"email": result["best_email"]}
            top = result["emails"][0] if result["emails"] else {}
            if top.get("name") and not lead.get("contact_name"):
                fields["contact_name"] = top["name"][:200]
            if top.get("title") and not lead.get("contact_title"):
                fields["contact_title"] = top["title"][:200]
            if result["phones"] and not lead.get("phone"):
                fields["phone"] = result["phones"][0][:200]
            db.update_lead(lead_id, fields)
            saved = True
        result["saved"] = saved
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── 验证公司真实性（web-check）─────────────────────────────────────
@app.route("/lead/<lead_id>/verify", methods=["POST"])
@onboarding_required
def verify_lead_company(lead_id):
    db   = _get_db(current_tid())
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"ok": False, "error": "客户不存在"}), 404
    try:
        from module1_collectors.company_verifier import CompanyVerifier
        report = CompanyVerifier().verify(website=lead.get("website", ""))
        return jsonify({"ok": True, "result": report})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── 深度调查（spiderfoot + LinkedIn 决策人）────────────────────────
@app.route("/lead/<lead_id>/investigate", methods=["POST"])
@onboarding_required
def investigate_lead(lead_id):
    tid  = current_tid()
    cfg  = current_cfg()
    db   = _get_db(tid)
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"ok": False, "error": "客户不存在"}), 404
    try:
        from module1_collectors.osint_investigator import OSINTInvestigator
        inv = OSINTInvestigator()
        inv.serper_key = cfg.get("serpapi_key", "")
        inv.hunter_key = cfg.get("hunter_api_key", "")
        report = inv.investigate(company_name=lead.get("company_name", ""),
                                 website=lead.get("website", ""),
                                 country=lead.get("country", ""))
        return jsonify({"ok": True, "result": report})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── 给客户发开发信（双通道）────────────────────────────────────────
@app.route("/lead/<lead_id>/send-email", methods=["POST"])
@onboarding_required
def send_lead_email(lead_id):
    tid  = current_tid()
    cfg  = current_cfg()
    db   = _get_db(tid)
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"ok": False, "error": "客户不存在"}), 404

    data    = request.get_json(silent=True) or {}
    to      = (data.get("to") or lead.get("email") or "").strip()
    subject = (data.get("subject") or "").strip()[:300]
    body    = (data.get("body") or "").strip()[:6000]
    if not to:
        return jsonify({"ok": False, "error": "该客户没有邮箱，请先用「一键找邮箱」补全"}), 400
    if not subject or not body:
        return jsonify({"ok": False, "error": "主题和正文不能为空"}), 400

    # 发信前验证邮箱真伪（设置里默认开启）
    if cfg.get("verify_before_send", True):
        try:
            from email_verifier import EmailVerifier
            ev = EmailVerifier().verify(to)
            if not ev["can_send"]:
                return jsonify({"ok": False,
                                "error": f"邮箱验证未通过（{ev['status']}）：{ev['reason']}",
                                "verify": ev}), 400
        except Exception as e:
            print(f"[send-email] 邮箱验证跳过: {e}")

    # 双通道发送（SMTP 通道注入打开/点击追踪；ESP 用其自带追踪）
    tracking = None
    if cfg.get("mail_channel", "smtp") == "smtp":
        try:
            trk_id = admin_db.create_tracking(tid, lead_id, subject)
            base = request.host_url.rstrip("/")
            tracking = {"open_url": f"{base}/t/o/{trk_id}.gif",
                        "click_base": f"{base}/t/c/{trk_id}?u="}
        except Exception as e:
            print(f"[send-email] 追踪创建失败: {e}")
    try:
        from tenant_mailer import TenantMailer
        mailer = TenantMailer(cfg)
        ok, info = mailer.send(to, subject, body,
                               to_name=lead.get("contact_name", ""),
                               tracking=tracking)
    except Exception as e:
        return jsonify({"ok": False, "error": f"发送失败：{e}"}), 500

    if not ok:
        return jsonify({"ok": False, "error": info}), 400

    # 记录外联历史 + 更新客户状态/邮箱
    db.log_outreach(lead_id, channel="email", direction="outbound",
                    subject=subject, content=body)
    upd = {"status": "contacted"}
    if not lead.get("email"):
        upd["email"] = to
    db.update_lead(lead_id, upd)
    # 登记自动跟进（没回复就到点自动发下一封）
    _enroll_followup_if_enabled(tid, cfg, lead_id)
    return jsonify({"ok": True, "info": f"{info}（{mailer.channel_label()}）"})


# ── 记录 WhatsApp 触达（点击 wa.me 后回报）──────────────────────────
@app.route("/lead/<lead_id>/log-whatsapp", methods=["POST"])
@onboarding_required
def log_lead_whatsapp(lead_id):
    db   = _get_db(current_tid())
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"ok": False, "error": "客户不存在"}), 404
    data = request.get_json(silent=True) or {}
    msg  = (data.get("message") or "").strip()[:2000]
    db.log_outreach(lead_id, channel="whatsapp", direction="outbound",
                    subject="WhatsApp", content=msg)
    db.update_lead(lead_id, {"status": "contacted"})
    return jsonify({"ok": True})


# ── WhatsApp 官方 API 自动发送（A 方案）──────────────────────────────
@app.route("/lead/<lead_id>/send-whatsapp-api", methods=["POST"])
@onboarding_required
def send_lead_whatsapp_api(lead_id):
    cfg  = current_cfg()
    db   = _get_db(current_tid())
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"ok": False, "error": "客户不存在"}), 404
    to = (lead.get("phone") or "").strip()
    if not to:
        return jsonify({"ok": False, "error": "该客户没有电话号码"}), 400
    msg = ((request.get_json(silent=True) or {}).get("message") or "").strip()[:2000]
    if not msg:
        return jsonify({"ok": False, "error": "消息内容为空"}), 400
    try:
        from whatsapp_sender import WhatsAppSender
        w = WhatsAppSender(cfg)
        if not w.is_configured():
            return jsonify({"ok": False,
                            "error": "未配置 WhatsApp 官方 API，请到设置页填写，或使用「通过 WhatsApp 联系」手动发送"}), 400
        ok, info = w.send(to, msg)
    except Exception as e:
        return jsonify({"ok": False, "error": f"发送失败：{e}"}), 500
    if not ok:
        return jsonify({"ok": False, "error": info}), 400
    db.log_outreach(lead_id, channel="whatsapp", direction="outbound",
                    subject="WhatsApp(API)", content=msg)
    db.update_lead(lead_id, {"status": "contacted"})
    return jsonify({"ok": True, "info": info})


# ─────────────────────────────────────────────────────────
# 独立站询盘插件（Inbound Lead Capture）
# ─────────────────────────────────────────────────────────

@app.route("/inbound")
@onboarding_required
def inbound_page():
    tid      = current_tid()
    token    = admin_db.get_or_create_inbound_token(tid)
    base     = request.host_url.rstrip("/")
    endpoint = f"{base}/api/inbound/{token}"
    return render_template("app/inbound.html", cfg=current_cfg(),
                           token=token, endpoint=endpoint, base=base)


@app.route("/inbound/regenerate", methods=["POST"])
@onboarding_required
def inbound_regenerate():
    admin_db.regenerate_inbound_token(current_tid())
    return redirect(url_for("inbound_page"))


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Max-Age"]       = "86400"
    return resp


@app.route("/api/inbound/<token>", methods=["POST", "OPTIONS"])
def api_inbound(token):
    """公开接收接口：客户独立站的询盘表单 POST 到这里，自动入库为线索。"""
    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)))
    # 防刷：每 token+IP 每分钟最多 60 条
    if not _rl.check(f"inbound:{token}:{_client_ip()}", 60, 60):
        return _cors(jsonify({"ok": False, "error": "too many requests"})), 429
    tid = admin_db.get_tid_by_inbound_token(token)
    if not tid:
        return _cors(jsonify({"ok": False, "error": "invalid token"})), 404

    data = request.get_json(silent=True) or request.form.to_dict() or {}

    def g(*keys):
        for k in keys:
            v = data.get(k)
            if v and str(v).strip():
                return str(v).strip()[:300]
        return ""

    email   = g("email", "Email", "e-mail", "mail", "your-email")
    name    = g("name", "Name", "fullname", "contact_name", "your-name")
    company = (g("company", "Company", "company_name", "organization")
               or name or (email.split("@")[0] if email else "") or "网站访客")
    message = g("message", "Message", "msg", "comment", "comments",
                "inquiry", "content", "your-message")[:1000]
    phone   = g("phone", "Phone", "tel", "mobile", "whatsapp")
    country = g("country", "Country")
    website = g("website", "Website", "url", "site")

    if not email and not phone:
        return _cors(jsonify({"ok": False, "error": "need email or phone"})), 400

    lead = {
        "company_name": company, "country": country, "email": email,
        "phone": phone, "contact_name": name, "website": website,
        "notes": ("[网站询盘] " + message) if message else "[网站询盘]",
        "source": "独立站询盘", "sources": ["inbound"], "status": "new",
    }
    try:
        from module2_cleaner import DataCleaner
        DataCleaner().run([lead], source="独立站询盘",
                          db_path=tenant_ctx.get_db_path(tid))
    except Exception as e:
        print(f"[inbound] 入库失败: {e}")
        return _cors(jsonify({"ok": False, "error": "server error"})), 500
    return _cors(jsonify({"ok": True}))


# ─────────────────────────────────────────────────────────
# 邮件打开 / 点击追踪（公开像素）
# ─────────────────────────────────────────────────────────

# 1x1 透明 GIF
_PIXEL = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00"
          b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01"
          b"\x00\x00\x02\x02D\x01\x00;")


@app.route("/t/o/<tracking_id>.gif")
def track_open(tracking_id):
    try:
        admin_db.record_open(tracking_id)
    except Exception as e:
        print(f"[track] open 记录失败: {e}")
    resp = Response(_PIXEL, mimetype="image/gif")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/t/c/<tracking_id>")
def track_click(tracking_id):
    try:
        admin_db.record_click(tracking_id)
    except Exception as e:
        print(f"[track] click 记录失败: {e}")
    target = request.args.get("u", "")
    if not target.startswith(("http://", "https://")):
        target = "https://" + target if target else "/"
    return redirect(target)


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
    try:
        email_stats = admin_db.get_open_stats(current_tid())
    except Exception:
        email_stats = {"sent": 0, "opened": 0, "clicked": 0,
                       "open_rate": 0.0, "click_rate": 0.0}
    return render_template("app/stats.html", cfg=current_cfg(),
                           stats=db.get_stats(),
                           email_stats=email_stats,
                           history=db.get_collection_history(limit=10))


# ─────────────────────────────────────────────────────────
# 采集渠道页面
# ─────────────────────────────────────────────────────────

@app.route("/collect")
@onboarding_required
def collect_page():
    return render_template("app/collect.html", cfg=current_cfg())


@app.route("/collect/ocr-cards", methods=["POST"])
@onboarding_required
def ocr_cards():
    """名片照片 AI 识别 —— 暂时下线。
    原用 DeepSeek 视觉模型 deepseek-vl，已被官方下线；V4(flash/pro) 不支持图片输入，
    暂无可用视觉模型。等接入新视觉服务（Claude / 阿里云 OCR）再恢复。"""
    return jsonify({"ok": False, "error":
        "名片照片识别暂时下线（视觉模型迁移中）。可先用上方「方式一：整理到 Excel/CSV 后导入」录入名片，"
        "或去「导入展会数据」批量导入。"})


@app.route("/collect/import-cards", methods=["POST"])
@onboarding_required
def import_cards():
    """把 OCR 识别结果导入数据库"""
    tid   = current_tid()
    data  = request.get_json(silent=True) or {}
    leads = data.get("leads", [])
    if not leads:
        return jsonify({"ok": False, "db_new": 0})
    from module2_cleaner import DataCleaner
    stats = DataCleaner().run(leads, source="展会名片",
                              db_path=tenant_ctx.get_db_path(tid))
    return jsonify({"ok": True, "db_new": stats.get("db_new", 0)})


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
    tid  = current_tid()
    cfg  = current_cfg()
    # 从 JSON body 获取用户勾选的渠道（collect页面传来），默认全跑
    body     = request.get_json(silent=True) or {}
    channels = body.get("channels", ["importyeti","google","apollo"])
    task_id  = f"{step}_{datetime.now().strftime('%H%M%S')}_{_uuid.uuid4().hex[:4]}"

    with _task_lock:
        _task_status[task_id] = {"status": "running", "log": [], "_ts": time.time()}

    def run_bg():
        logs      = []
        countries = cfg.get("target_countries", [])[:20]
        # 平台 Serper 额度闸：超额则本次不跑需要 Serper 的渠道（google/google_maps）
        from api_platform import serper_keys_for
        _sk, _splat, _sblocked, _sreason = serper_keys_for(tid, cfg)
        if _sblocked:
            cfg["serpapi_key"] = ""        # 让 google/maps 等渠道优雅跳过 Serper
            logs.append("⚠️ " + _sreason)
        try:
            from module2_cleaner import DataCleaner

            # ── ImportYeti ──────────────────────────────────
            if "importyeti" in channels and step in ("collect","all"):
                from module1_collectors.importyeti import ImportYetiCollector
                col = ImportYetiCollector()
                col.api_key = cfg.get("importyeti_api_key", "")
                col.mode    = "api" if col.api_key else "scrape"
                raw = col.fetch_all(mock=not col.api_key)
                if raw:
                    s = DataCleaner().run(raw, source="importyeti",
                                         db_path=tenant_ctx.get_db_path(tid))
                    logs.append(f"ImportYeti: 新增 {s.get('db_new',0)} 条")

            # ── Google + DeepSeek ───────────────────────────
            if "google" in channels and step in ("collect","all"):
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
                        logs.append(f"谷歌搜索: 新增 {gs.get('db_new',0)} 条")

            # ── Zauba（印度海关）────────────────────────────
            if "zauba" in channels and step in ("collect","all"):
                from module1_collectors.zauba import ZaubaCollector
                zc = ZaubaCollector()
                zc.product_name    = cfg.get("product_name", "")
                zc.search_keywords = cfg.get("search_keywords", [])
                zc.hs_codes        = cfg.get("hs_codes", ["8407"])
                z_raw = zc.fetch_all(mock=False)
                if z_raw:
                    z_s = DataCleaner().run(z_raw, source="zauba",
                                           db_path=tenant_ctx.get_db_path(tid))
                    logs.append(f"Zauba印度: 新增 {z_s.get('db_new',0)} 条")
                else:
                    logs.append("Zauba: 采集失败")

            # ── Google Maps ─────────────────────────────────
            if "google-maps" in channels and step in ("collect","all"):
                from module1_collectors.google_maps import GoogleMapsCollector
                mc = GoogleMapsCollector()
                mc.serper_key      = cfg.get("serpapi_key", "")
                mc.product_name    = cfg.get("product_name", "")
                mc.search_keywords = cfg.get("search_keywords", [])
                gm_raw = mc.fetch_all(countries=countries[:10], mock=False)
                if gm_raw:
                    gm_s = DataCleaner().run(gm_raw, source="google_maps",
                                            db_path=tenant_ctx.get_db_path(tid))
                    logs.append(f"Google Maps: 新增 {gm_s.get('db_new',0)} 条")
                else:
                    logs.append("Google Maps: 采集失败")

            # ── Europages（欧洲B2B目录）────────────────────
            if "europages" in channels and step in ("collect","all"):
                from module1_collectors.europages import EuropagesCollector
                ec = EuropagesCollector()
                ec.product_name    = cfg.get("product_name", "")
                ec.search_keywords = cfg.get("search_keywords", [])
                ep_raw = ec.fetch_all(mock=False)
                if ep_raw:
                    ep_s = DataCleaner().run(ep_raw, source="europages",
                                            db_path=tenant_ctx.get_db_path(tid))
                    logs.append(f"Europages欧洲: 新增 {ep_s.get('db_new',0)} 条")
                else:
                    logs.append("Europages: 采集失败")

            # ── 阿里巴巴 RFQ ────────────────────────────────
            if "alibaba-rfq" in channels and step in ("collect","all"):
                from module1_collectors.alibaba_rfq import AlibabaRFQCollector
                rc = AlibabaRFQCollector()
                rc.product_name    = cfg.get("product_name", "")
                rc.search_keywords = cfg.get("search_keywords", [])
                rc.deepseek_key    = cfg.get("deepseek_api_key", "")
                rfq_raw = rc.fetch_all(mock=False)
                if rfq_raw:
                    rfq_s = DataCleaner().run(rfq_raw, source="alibaba_rfq",
                                             db_path=tenant_ctx.get_db_path(tid))
                    logs.append(f"阿里RFQ: 新增 {rfq_s.get('db_new',0)} 条")
                else:
                    logs.append("阿里RFQ: 采集失败")

            # ── Apollo.io ───────────────────────────────────
            if "apollo" in channels and step in ("collect","all"):
                apollo_key = cfg.get("apollo_api_key", "")
                if apollo_key:
                    from module1_collectors.apollo import ApolloCollector
                    ac = ApolloCollector()
                    ac.api_key         = apollo_key
                    ac.product_name    = cfg.get("product_name", "")
                    ac.search_keywords = cfg.get("search_keywords", [])
                    a_raw = ac.fetch_all(countries=countries[:5])
                    if a_raw:
                        a_s = DataCleaner().run(a_raw, source="apollo",
                                               db_path=tenant_ctx.get_db_path(tid))
                        logs.append(f"Apollo: 新增 {a_s.get('db_new',0)} 条")
                else:
                    logs.append("Apollo: 未配置 API Key，跳过")

            # ── YouTube（评论区/简介获客）───────────────────
            if "youtube" in channels and step in ("collect", "all"):
                from module1_collectors.youtube import YouTubeCollector
                yc = YouTubeCollector()
                yc.api_key         = cfg.get("youtube_api_key", "")
                yc.deepseek_key    = cfg.get("deepseek_api_key", "")
                yc.product_name    = cfg.get("product_name", "")
                yc.search_keywords = cfg.get("search_keywords", [])
                yt_raw = yc.fetch_all(mock=False)
                if yt_raw:
                    yt_s = DataCleaner().run(yt_raw, source="youtube",
                                             db_path=tenant_ctx.get_db_path(tid))
                    logs.append(f"YouTube: 新增 {yt_s.get('db_new',0)} 条")
                else:
                    logs.append("YouTube: 未找到线索（需配 YouTube API Key 或装 yt-dlp）")

            # ── TikTok（Apify）──────────────────────────────
            if "tiktok" in channels and step in ("collect", "all"):
                apify_token = cfg.get("apify_token", "")
                if apify_token:
                    from module1_collectors.tiktok import TikTokCollector
                    tc = TikTokCollector()
                    tc.apify_token     = apify_token
                    tc.product_name    = cfg.get("product_name", "")
                    tc.search_keywords = cfg.get("search_keywords", [])
                    tt_raw = tc.fetch_all(mock=False)
                    if tt_raw:
                        tt_s = DataCleaner().run(tt_raw, source="tiktok",
                                                 db_path=tenant_ctx.get_db_path(tid))
                        logs.append(f"TikTok: 新增 {tt_s.get('db_new',0)} 条")
                    else:
                        logs.append("TikTok: 未找到线索")
                else:
                    logs.append("TikTok: 未配置 Apify Token，跳过")

            # ── 批量补全邮箱（给有官网/主页但没邮箱的线索，含社交线索）──
            if step in ("enrich", "all"):
                from module1_collectors.email_enricher import EmailEnricher
                en = EmailEnricher()
                en.serper_key = cfg.get("serpapi_key", "")
                en.hunter_key = cfg.get("hunter_api_key", "")
                db_e = _get_db(tid)
                with db_e.get_conn() as conn:
                    rows = conn.execute(
                        """SELECT id, company_name, website FROM leads
                           WHERE website IS NOT NULL AND TRIM(website) != ''
                             AND (email IS NULL OR TRIM(email) = '')
                           ORDER BY final_score DESC LIMIT 25"""
                    ).fetchall()
                filled = 0
                for r in rows:
                    try:
                        res = en.enrich(website=r["website"],
                                        company_name=r["company_name"] or "")
                        if res.get("best_email"):
                            fields = {"email": res["best_email"]}
                            if res["phones"]:
                                fields["phone"] = res["phones"][0][:200]
                            top = res["emails"][0] if res["emails"] else {}
                            if top.get("name"):
                                fields["contact_name"] = top["name"][:200]
                            if top.get("title"):
                                fields["contact_title"] = top["title"][:200]
                            db_e.update_lead(r["id"], fields)
                            filled += 1
                    except Exception as e:
                        print(f"[enrich] {r['company_name']}: {e}")
                logs.append(f"批量找邮箱: 补全 {filled}/{len(rows)} 条")

            # ── 评分 ────────────────────────────────────────
            if step in ("score", "all"):
                from module3_scorer import LeadScorer
                s = LeadScorer(db_path=tenant_ctx.get_db_path(tid)).run(use_ai=False)
                logs.append(f"AI评分: A={s.get('grade_A',0)} B={s.get('grade_B',0)}")

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
# 渠道雷达（竞品经销商挖掘 + 情报卡 + 定期监控）
# ─────────────────────────────────────────────────────────

def _radar_snapshot(distributors: list, intel: dict) -> str:
    """根据经销商名单 + 情报卡算一个指纹，用来比对竞品是否有变化。"""
    import hashlib
    names = sorted((d.get("company_name") or "").strip().lower()
                   for d in (distributors or []))
    blob = "|".join(names) + "##" + (intel or {}).get("main_products", "")
    return hashlib.md5(blob.encode("utf-8", "ignore")).hexdigest()


@app.route("/radar")
@onboarding_required
def radar():
    tid = current_tid()
    cfg = current_cfg()
    sort = request.args.get("sort", "")
    all_comps = admin_db.list_competitors(tid)
    if sort == "dist":          # 按经销商数排序，看哪个竞品最能产线索
        all_comps = sorted(all_comps,
                           key=lambda c: c.get("distributor_count") or 0, reverse=True)
    per_page  = 12
    total     = len(all_comps)
    pages     = max(1, (total + per_page - 1) // per_page)
    cp        = min(max(1, int(request.args.get("cp", 1) or 1)), pages)
    competitors = all_comps[(cp - 1) * per_page: cp * per_page]
    has_keys  = bool(cfg.get("deepseek_api_key"))
    return render_template("app/radar.html", competitors=competitors,
                           cfg=cfg, has_keys=has_keys, comp_sort=sort,
                           comp_total=total, comp_page=cp, comp_pages=pages)


@app.route("/radar/cleanup", methods=["POST"])
@onboarding_required
def radar_cleanup():
    """清理"单次扫描"的竞品卡（status=once，从没开监控的），收拾列表。已入库线索不动。"""
    n = admin_db.delete_once_competitors(current_tid())
    return jsonify({"ok": True, "deleted": n})


@app.route("/radar/run", methods=["POST"])
@onboarding_required
def radar_run():
    _cleanup_tasks()
    tid  = current_tid()
    cfg  = current_cfg()
    body = request.get_json(silent=True) or {}
    raw_urls    = body.get("urls", "")
    auto_search = bool(body.get("auto_search"))
    # 监控改为按竞品单独开启（扫描只跑一次，竞品先以 once 入列表，
    # 用户在列表里挑真正想长期盯的逐个开「定期监控」）
    monitor     = bool(body.get("monitor", False))
    freq        = int(body.get("frequency_days", 7) or 7)
    # 四源开关（前端勾选），缺省四个都开
    sources     = body.get("sources")
    if not isinstance(sources, list) or not sources:
        sources = ["reverse", "social", "website", "customs"]
    # 本次重点扫描国家（客户从下拉选或手填）：用 Google 该国本地索引精准搜，治"结果全是热门市场"
    focus_country = (body.get("focus_country") or "").strip()[:40]
    if focus_country:
        from module1_collectors.competitor_radar import resolve_focus_country
        if not resolve_focus_country(focus_country):
            return jsonify({"ok": False, "error":
                f"没认出国家「{focus_country}」。请从下拉里选，"
                "或填标准国家名（如 尼日利亚 / Nigeria）。"}), 400

    # 解析用户填的网址（换行/逗号分隔）
    urls = []
    if isinstance(raw_urls, list):
        urls = [u for u in raw_urls if u]
    elif isinstance(raw_urls, str):
        urls = [u.strip() for u in re.split(r"[\n,;\s]+", raw_urls) if u.strip()]

    # 拦掉明显不是竞品官网的门户/平台/社交站（新浪/Instagram/淘宝…），别浪费额度也别进竞品列表
    from module1_collectors.competitor_radar import is_invalid_competitor_url
    bad_urls = [u for u in urls if is_invalid_competitor_url(u)]
    urls     = [u for u in urls if not is_invalid_competitor_url(u)]

    if not urls and not auto_search:
        if bad_urls:
            return jsonify({"ok": False, "error":
                "你填的不是竞品官网（新浪/Instagram/淘宝等门户·平台·社交站不是竞品）。"
                "请填同行公司的官网首页，例如 loncin.com、zongshen.com。"}), 400
        return jsonify({"ok": False, "error": "请填竞品网址，或勾选「让系统自动搜竞品」"}), 400
    if not cfg.get("deepseek_api_key"):
        return jsonify({"ok": False, "error": "渠道雷达需要先在「系统设置」填 DeepSeek API Key"}), 400

    task_id = f"radar_{datetime.now().strftime('%H%M%S')}_{_uuid.uuid4().hex[:4]}"
    with _task_lock:
        _task_status[task_id] = {"status": "running", "log": [], "_ts": time.time()}

    def run_bg():
        logs = []
        if bad_urls:
            logs.append("已跳过非竞品网址（门户/平台/社交站，不会扫）："
                        + "、".join(bad_urls[:5]))
        try:
            from module1_collectors.competitor_radar import CompetitorRadar
            from module2_cleaner import DataCleaner

            from api_platform import serper_keys_for, record_serper
            s_keys, s_platform, s_blocked, s_reason = serper_keys_for(tid, cfg)
            if s_blocked:
                logs.append("⚠️ " + s_reason + "（本次只跑官网扫描）")
            radar_eng = CompetitorRadar()
            radar_eng.deepseek_key    = cfg.get("deepseek_api_key", "")
            radar_eng.serper_keys     = s_keys        # 平台代付（限额）或 BYOK（不限），超额则为空
            radar_eng.hunter_key      = cfg.get("hunter_api_key", "")
            radar_eng.product_name    = cfg.get("product_name", "")
            radar_eng.search_keywords = cfg.get("search_keywords", [])
            radar_eng.target_countries = cfg.get("target_countries", [])
            radar_eng.focus_country   = focus_country
            radar_eng.sources         = sources
            radar_eng.proxy           = cfg.get("radar_proxy", "")
            _prof = cfg.get("product_profile") or {}
            radar_eng.my_category     = _prof.get("category") or cfg.get("product_name", "")
            radar_eng.product_i18n    = _prof.get("keywords_i18n") or {}
            if focus_country:
                logs.append(f"🎯 本次重点扫描国家：{focus_country}（用 Google 该国本地索引精准搜）")

            out = radar_eng.run(urls=urls, auto_search=auto_search,
                                want_intel=True, max_competitors=8)
            # 平台代付时记录 Serper 本月用量（BYOK 不计入平台额度）
            record_serper(tid, cfg, out.get("serper_calls", 0))

            # 三个搜索源（反向/电商社媒/海关反查）都靠 Serper，没配就只剩官网扫描
            search_srcs = [s for s in sources if s in ("reverse", "social", "customs")]
            if search_srcs and not cfg.get("serpapi_key"):
                logs.append("⚠️ 反向搜索/电商社媒/海关反查都需要 Serper Key，"
                            "当前未配置，本次只跑了官网扫描。去「系统设置」填 Serper Key 可大幅提升挖掘量。")

            dist = out.get("distributors", [])
            if dist:
                st = DataCleaner().run(dist, source="competitor_radar",
                                       db_path=tenant_ctx.get_db_path(tid))
                auto_n   = sum(1 for d in dist if d.get("status") == "new")
                review_n = len(dist) - auto_n
                msg = (f"渠道商入库：新增 {st.get('db_new',0)} 家"
                       f"（{len(sources)} 个数据源共提取 {len(dist)} 条）"
                       f" · 高匹配 {auto_n} 条直接入库")
                if review_n:
                    msg += (f"，{review_n} 条「待确认」已入库待你核对"
                            f"（客户列表筛选状态=待确认）")
                logs.append(msg)
            else:
                logs.append("未提取到合格渠道商（低匹配的已被质量闸过滤；"
                            "竞品可能尚无公开经销商，或全网/海关暂无其品牌线索）")

            # 保存竞品情报卡 + 纳入监控（带「新增了谁」diff）
            intel_by_host = {c.get("competitor"): c for c in out.get("intel", [])}
            for site in out.get("sites", []):
                from urllib.parse import urlparse
                host = urlparse(site).netloc.replace("www.", "")
                card = intel_by_host.get(host, {})
                site_dist = [d for d in dist
                             if host in (d.get("notes") or "")]
                dist_names = [f"{d['company_name']}({d.get('country') or '?'})"
                              for d in site_dist]
                snap = _radar_snapshot(site_dist, card)
                admin_db.upsert_competitor(
                    tid, url=site, host=host,
                    name=card.get("main_products", "")[:80],
                    intel=card, distributor_count=len(site_dist),
                    snapshot_hash=snap, frequency_days=freq, monitor=monitor,
                    distributor_names=dist_names)
            logs.append(f"竞品情报卡：{len(out.get('intel', []))} 张"
                        + ("，已纳入定期监控" if monitor
                           else "（已存入下方列表，挑你想长期盯的单独开启「定期监控」）"))

            calls = out.get("serper_calls", 0)
            fails = out.get("serper_fails", 0)
            if calls or fails:
                if calls == 0 and fails:
                    logs.append(f"❌ Serper 搜索全部失败（{fails} 次，key 无效/额度用尽？）"
                                "——反向/社媒/海关源都没出数据。请检查系统设置里的 Serper Key；"
                                "若填了多个 key 会自动切换，全失败说明都不可用。")
                elif fails:
                    logs.append(f"本次消耗 Serper 搜索 {calls} 次（另有 {fails} 次失败已自动切 key/跳过）")
                else:
                    logs.append(f"本次消耗 Serper 搜索 {calls} 次")

            if out.get("errors"):
                logs.append("部分竞品处理出错：" + "；".join(out["errors"][:3]))

            with _task_lock:
                _task_status[task_id] = {"status": "done", "log": logs,
                                         "_ts": time.time()}
        except Exception as e:
            with _task_lock:
                _task_status[task_id] = {"status": "error", "log": [str(e)],
                                         "_ts": time.time()}

    threading.Thread(target=run_bg, daemon=True).start()
    return jsonify({"ok": True, "task_id": task_id})


@app.route("/radar/<cid>/monitor", methods=["POST"])
@onboarding_required
def radar_monitor(cid):
    tid  = current_tid()
    body = request.get_json(silent=True) or {}
    on   = bool(body.get("monitor", True))
    freq = body.get("frequency_days")
    freq = int(freq) if freq else None
    admin_db.set_competitor_monitor(tid, cid, on, frequency_days=freq)
    return jsonify({"ok": True})


@app.route("/radar/<cid>/seen", methods=["POST"])
@onboarding_required
def radar_seen(cid):
    admin_db.clear_competitor_change(current_tid(), cid)
    return jsonify({"ok": True})


@app.route("/radar/<cid>/delete", methods=["POST"])
@onboarding_required
def radar_delete(cid):
    admin_db.delete_competitor(current_tid(), cid)
    return jsonify({"ok": True})


@app.route("/api/quota")
@onboarding_required
def api_quota_view():
    """各 API 服务额度（工作台「API 额度」卡 AJAX 拉取）。
    Serper/DeepSeek 默认平台代付（显示本月用量 / 会员档上限）；客户填了自己 key 则显示 BYOK。
    Hunter 仅 BYOK，查真实余额。用 raw config 判断到底是平台还是 BYOK。"""
    tid = current_tid()
    cfg = tenant_ctx.load_config(tid)                  # raw：区分平台/BYOK
    from api_quota import deepseek_balance, hunter_account, parse_keys
    from api_platform import quota_status
    qs = quota_status(tid, cfg)
    out = {}
    # Serper：平台代付（用量/上限）或 BYOK
    sp = qs["serper"]
    if sp["source"] == "platform":
        out["serper"] = {"source": "platform", "tier": sp["tier"],
                         "used": sp["used"], "limit": sp["limit"],
                         "remaining": sp["remaining"]}
    else:
        out["serper"] = {"source": "byok", "keys": sp["keys"]}
    # DeepSeek：BYOK 查真实余额，平台代付显示"平台提供"
    if qs["deepseek"]["source"] == "byok":
        ds = parse_keys(cfg.get("deepseek_api_key", ""))
        bal = deepseek_balance(ds[0]) if ds else {"ok": False, "reason": "未配置"}
        bal["source"] = "byok"
        out["deepseek"] = bal
    else:
        out["deepseek"] = {"source": "platform"}
    # Hunter：仅 BYOK
    hu = parse_keys(cfg.get("hunter_api_key", ""))
    out["hunter"] = hunter_account(hu[0]) if hu else {"ok": False, "reason": "未配置"}
    return jsonify(out)


# ─────────────────────────────────────────────────────────
# 系统设置
# ─────────────────────────────────────────────────────────

@app.route("/product-profile/generate", methods=["POST"])
@onboarding_required
def product_profile_generate():
    """AI 起草「产品搜索画像」：客户描述/产品页 → DeepSeek 结构化搜索词草稿。"""
    cfg  = current_cfg()
    body = request.get_json(silent=True) or {}
    desc = (body.get("description") or "").strip()[:1200]
    url  = (body.get("url") or "").strip()[:300]
    if not cfg.get("deepseek_api_key"):
        return jsonify({"ok": False, "error": "请先在系统设置填 DeepSeek API Key"}), 400
    if not desc and not url:
        return jsonify({"ok": False, "error": "请先填产品描述，或贴一个产品页网址"}), 400
    try:
        from product_profiler import generate_profile
        res = generate_profile(cfg["deepseek_api_key"], desc, url,
                               cfg.get("target_countries"), cfg.get("radar_proxy", ""))
    except Exception as e:
        return jsonify({"ok": False, "error": f"生成失败：{e}"}), 500
    return jsonify(res)


@app.route("/settings", methods=["GET", "POST"])
@onboarding_required
def settings():
    tid   = current_tid()
    # 用 raw config（不注入平台 key）：设置页显示客户自己填的真实状态，
    # 保存时也不会把平台主 key 误写进 config.json。
    cfg   = tenant_ctx.load_config(tid)
    saved = False
    if request.method == "POST":
        # ── 第二批：公司/产品/目标市场（原本只能在入驻向导填，现可随时改）──
        if request.form.get("save_profile") == "1":
            cfg["company_name"] = request.form.get("company_name", "").strip()[:100]
            cfg["industry"]     = request.form.get("industry", cfg.get("industry", ""))
            cfg["product_name"] = request.form.get("product_name", "").strip()[:100]
            cfg["product_desc"] = request.form.get("product_desc", "").strip()[:500]
            hs_raw = request.form.get("hs_codes", "")
            cfg["hs_codes"] = [h.strip() for h in
                               hs_raw.replace("，", ",").split(",") if h.strip()][:20]
            # 搜索关键词改由「产品搜索画像」派生（见下）；没画像时保留原有 search_keywords 不动
            # 产品搜索画像（AI 起草 + 客户确认，结构化搜索词库）
            try:
                prof = json.loads(request.form.get("product_profile_json", "") or "{}")
            except Exception:
                prof = {}
            if isinstance(prof, dict) and prof.get("keywords_en"):
                # 规整存储
                def _plist(key, n):
                    v = prof.get(key) or []
                    return [str(x).strip() for x in v if str(x).strip()][:n]
                i18n = prof.get("keywords_i18n") or {}
                clean_i18n = {}
                if isinstance(i18n, dict):
                    for lg, terms in i18n.items():
                        terms = [str(x).strip() for x in (terms or []) if str(x).strip()][:6]
                        if terms:
                            clean_i18n[lg] = terms
                cfg["product_profile"] = {
                    "category":     str(prof.get("category", "")).strip()[:120],
                    "keywords_en":  _plist("keywords_en", 12),
                    "synonyms":     _plist("synonyms", 12),
                    "models":       _plist("models", 10),
                    "applications": _plist("applications", 8),
                    "buyer_types":  _plist("buyer_types", 6),
                    "hs_suggested": _plist("hs_suggested", 8),
                    "keywords_i18n": clean_i18n,
                }
                # 派生 search_keywords：英文词+同义词+型号 → 所有采集器零改动受益
                derived = []
                for grp in ("keywords_en", "synonyms", "models"):
                    for w in cfg["product_profile"][grp]:
                        if w not in derived:
                            derived.append(w)
                if derived:
                    cfg["search_keywords"] = derived[:30]
                # HS 没手填则用 AI 建议的兜底
                if not cfg["hs_codes"] and cfg["product_profile"]["hs_suggested"]:
                    cfg["hs_codes"] = cfg["product_profile"]["hs_suggested"][:20]
            try:
                selected_regions   = json.loads(request.form.get("selected_regions", "[]"))
                excluded_countries = json.loads(request.form.get("excluded_countries", "[]"))
            except Exception:
                selected_regions, excluded_countries = [], []
            if selected_regions:
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
            session["company_name"] = cfg["company_name"]
            admin_db.update_tenant(tid, company_name=cfg["company_name"],
                                   industry=cfg["industry"])
            tenant_ctx.save_config(tid, cfg)
            return render_template("app/settings.html", cfg=cfg, saved=True,
                                   regions=tenant_ctx.REGIONS,
                                   industries=tenant_ctx.INDUSTRY_OPTIONS)
        for k in ("importyeti_api_key", "serpapi_key", "hunter_api_key",
                  "deepseek_api_key", "anthropic_api_key", "apollo_api_key",
                  "youtube_api_key", "apify_token", "radar_proxy",
                  "smtp_user", "smtp_pass", "sender_name", "email_from_name",
                  # 双通道发信
                  "smtp_host", "smtp_port", "smtp_from_name", "smtp_from_email",
                  "esp_provider", "esp_api_key", "esp_from_email", "esp_from_name",
                  "esp_domain", "esp_region",
                  # WhatsApp 官方 API（A 方案，可选）
                  "wa_provider", "wa_account_sid", "wa_auth_token", "wa_from",
                  "wa_api_key", "wa_phone_id", "wa_token"):
            v = request.form.get(k, "").strip()[:200]
            if v:
                cfg[k] = v
        # 可填多个 key（换行/逗号分隔，一个用尽自动切下一个）→ 放宽长度上限
        for k in ("serpapi_key", "deepseek_api_key", "hunter_api_key"):
            v = request.form.get(k, "").strip()[:2000]
            if v:
                cfg[k] = v
        # Serper 套餐额度（每个 key 每月，用于「API 额度」卡估算剩余；纯数字）
        sq = request.form.get("serper_quota", "").strip().replace(",", "")
        if sq.isdigit():
            cfg["serper_quota"] = int(sq)
        elif request.form.get("serper_quota") is not None and not sq:
            cfg["serper_quota"] = 0
        # 签名可较长，单独处理
        sig = request.form.get("email_signature", "").strip()[:1000]
        if sig:
            cfg["email_signature"] = sig
        # 发信通道（单选）+ 发信前验证（开关）
        cfg["mail_channel"]       = request.form.get("mail_channel", "smtp")
        cfg["verify_before_send"] = request.form.get("verify_before_send") == "on"
        # 自动跟进序列
        cfg["followup_enabled"]   = request.form.get("followup_enabled") == "on"
        for k in ("followup_days", "followup_days2"):
            v = request.form.get(k, "").strip()
            if v.isdigit():
                cfg[k] = int(v)
        cfg["ai_enabled"]    = request.form.get("AI_ENABLED") == "on"
        cfg["email_ai_mode"] = request.form.get("EMAIL_AI_MODE") == "on"
        tenant_ctx.save_config(tid, cfg)
        session["company_name"] = cfg.get("company_name", "")
        saved = True
    return render_template("app/settings.html", cfg=cfg, saved=saved,
                           regions=tenant_ctx.REGIONS,
                           industries=tenant_ctx.INDUSTRY_OPTIONS)


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


def _load_tenant_templates(tid: str, cfg: dict) -> dict:
    """加载租户邮件模板，没有则返回默认模板。"""
    tpl_path = tenant_ctx.get_email_templates_path(tid)
    if tpl_path.exists():
        try:
            return json.loads(tpl_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return _default_email_templates(cfg)


def _fill_placeholders(text: str, lead: dict, cfg: dict) -> str:
    """把开发信模板里的占位符替换成该客户的真实信息。"""
    repl = {
        "{company_name}": lead.get("company_name", "") or "",
        "{contact_name}": lead.get("contact_name") or "there",
        "{country}":      lead.get("country", "") or "",
        "{sender_name}":  cfg.get("sender_name") or cfg.get("email_from_name") or "",
        "{product_name}": cfg.get("product_name", "") or "",
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    return text


def _render_templates_for_lead(tid: str, cfg: dict, lead: dict) -> dict:
    """返回 {key: {display_name, subject, body}}，subject/body 已填好该客户信息。"""
    tpls = _load_tenant_templates(tid, cfg)
    sig  = cfg.get("email_signature", "")
    out  = {}
    for key, t in tpls.items():
        body = _fill_placeholders(t.get("body", ""), lead, cfg)
        if sig and sig.strip() and sig.strip() not in body:
            body = body.rstrip() + "\n\n" + sig
        out[key] = {
            "display_name": t.get("display_name", key),
            "subject": _fill_placeholders(t.get("subject", ""), lead, cfg),
            "body": body,
        }
    return out


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


@app.route("/admin/mail-test", methods=["POST"])
@admin_required
def admin_mail_test():
    """测试系统发信通道（注册/验证邮件用的 SMTP）。配 DirectMail 后点这个一键验证，
    不用每次去注册假账号。返回当前 SMTP 配置 + 成功/失败原因。"""
    to = request.form.get("to") or (request.get_json(silent=True) or {}).get("to") or ""
    to = to.strip()
    if "@" not in to:
        return jsonify({"ok": False, "detail": "请填一个有效的收件邮箱"}), 400
    from mailer import smtp_diagnose
    return jsonify(smtp_diagnose(to))


@app.route("/admin/pipeline")
@admin_required
def admin_pipeline():
    from pipeline_config import PIPELINE_STAGES, STATUS_META, pipeline_counts
    return render_template("admin/pipeline.html",
                           stages=PIPELINE_STAGES, meta=STATUS_META,
                           counts=pipeline_counts())


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


@app.route("/admin/tenant/create", methods=["POST"])
@admin_required
def admin_create_tenant_route():
    email    = request.form.get("email", "").strip().lower()[:120]
    password = request.form.get("password", "").strip()[:120]
    company  = request.form.get("company_name", "").strip()[:100]
    status   = request.form.get("status", "active")
    if not email or not password:
        flash("邮箱和密码必填")
        return redirect(url_for("admin_panel"))
    r = admin_db.admin_create_tenant(email, password, company, status)
    flash(f"✅ 账号已创建：{email}（已激活，可直接登录）"
          if r["ok"] else f"创建失败：{r['error']}")
    return redirect(url_for("admin_panel"))


@app.route("/admin/tenant/<tid>/edit", methods=["POST"])
@admin_required
def admin_edit_tenant(tid):
    email    = request.form.get("email", "").strip().lower()[:120]
    company  = request.form.get("company_name", "").strip()[:100]
    password = request.form.get("password", "").strip()[:120]
    r = admin_db.admin_update_tenant(tid, email=email or None,
                                     company_name=company,
                                     password=password or None)
    flash("✅ 账号已更新" if r["ok"] else f"更新失败：{r['error']}")
    return redirect(url_for("admin_panel"))


@app.route("/admin/tenant/<tid>/delete", methods=["POST"])
@admin_required
def admin_delete_tenant(tid):
    admin_db.delete_tenant(tid)
    flash("🗑️ 账号及其全部数据已删除")
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
# 自动跟进序列（后台引擎）
# ─────────────────────────────────────────────────────────

def _utc_after(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)
            ).strftime("%Y-%m-%d %H:%M:%S")


def _public_base() -> str:
    """追踪像素/点击的公网前缀。请求上下文里用当前域名，后台线程用 config.SITE_URL。"""
    try:
        return request.host_url.rstrip("/")
    except Exception:
        pass
    try:
        import config
        return (getattr(config, "SITE_URL", "") or "").rstrip("/")
    except Exception:
        return ""


def _followup_steps(cfg: dict) -> list:
    """根据租户设置生成跟进步骤。默认：首封后3天发一次跟进。"""
    steps = []
    try:
        d1 = int(cfg.get("followup_days") or 3)
    except Exception:
        d1 = 3
    steps.append({"template": "follow_up", "days": max(1, d1)})
    try:
        d2 = int(cfg.get("followup_days2") or 0)
    except Exception:
        d2 = 0
    if d2 > 0:
        steps.append({"template": "follow_up", "days": d2})
    return steps


def _enroll_followup_if_enabled(tid: str, cfg: dict, lead_id: str) -> None:
    if not cfg.get("followup_enabled"):
        return
    steps = _followup_steps(cfg)
    if steps:
        try:
            admin_db.enroll_followup(tid, lead_id, _utc_after(steps[0]["days"]))
        except Exception as e:
            print(f"[followup] 登记失败: {e}")


def process_due_followups() -> int:
    """扫描到点的跟进，没回复就自动发下一封。返回处理条数。"""
    try:
        due = admin_db.get_due_followups()
    except Exception as e:
        print(f"[followup] 读取失败: {e}")
        return 0
    handled = 0
    for f in due:
        fid, tid, lead_id, step = f["id"], f["tenant_id"], f["lead_id"], f["step"]
        try:
            cfg = tenant_ctx.load_config(tid)
            if not cfg.get("followup_enabled"):
                admin_db.finish_followup(fid, "stopped"); continue
            steps = _followup_steps(cfg)
            if step >= len(steps):
                admin_db.finish_followup(fid, "done"); continue
            db   = _get_db(tid)
            lead = db.get_lead(lead_id)
            if not lead:
                admin_db.finish_followup(fid, "done"); continue
            # 已回复/成交/拒绝 → 停止跟进
            if lead.get("status") in ("replied", "converted", "rejected"):
                admin_db.finish_followup(fid, "stopped"); continue
            to = (lead.get("email") or "").strip()
            if not to:
                admin_db.finish_followup(fid, "stopped"); continue

            tpls    = _render_templates_for_lead(tid, cfg, lead)
            tpl     = tpls.get(steps[step]["template"]) or {}
            subject = tpl.get("subject") or "Following up"
            body    = tpl.get("body") or ""

            tracking = None
            base = _public_base()
            if cfg.get("mail_channel", "smtp") == "smtp" and base:
                trk = admin_db.create_tracking(tid, lead_id, subject)
                tracking = {"open_url": f"{base}/t/o/{trk}.gif",
                            "click_base": f"{base}/t/c/{trk}?u="}

            from tenant_mailer import TenantMailer
            ok, info = TenantMailer(cfg).send(
                to, subject, body, to_name=lead.get("contact_name", ""),
                tracking=tracking)

            if ok:
                db.log_outreach(lead_id, channel="email", direction="outbound",
                                subject=subject, content=body)
                nxt = step + 1
                if nxt < len(steps):
                    admin_db.advance_followup(fid, nxt, _utc_after(steps[nxt]["days"]))
                else:
                    admin_db.finish_followup(fid, "done")
                handled += 1
                print(f"[followup] 自动跟进已发：{lead.get('company_name','?')} → {to}")
            else:
                # 发送失败（多为通道未配置）→ 推迟1天重试，连续失败可人工排查
                admin_db.postpone_followup(fid, _utc_after(1))
                print(f"[followup] 发送失败，已推迟：{info}")
        except Exception as e:
            print(f"[followup] 处理异常 {fid}: {e}")
    return handled


_followup_started = False


def start_followup_scheduler(interval: int = 1800) -> None:
    """启动后台跟进调度线程（每 interval 秒扫一次）。重复调用安全。"""
    global _followup_started
    if _followup_started:
        return
    _followup_started = True

    def _loop():
        while True:
            time.sleep(interval)
            try:
                process_due_followups()
            except Exception as e:
                print(f"[followup] 调度异常: {e}")

    threading.Thread(target=_loop, daemon=True).start()
    print("[followup] 自动跟进调度已启动")


# 模块加载即启动（run.py 导入或 WSGI 部署都会触发）
start_followup_scheduler()


# ─────────────────────────────────────────────────────────
# 渠道雷达：竞品定期监控（后台调度）
# ─────────────────────────────────────────────────────────

def process_due_competitors() -> int:
    """重扫到点的监控竞品，更新情报卡 + 入库新经销商 + 标记变化。"""
    due = admin_db.get_due_competitors(limit=20)
    if not due:
        return 0
    from module1_collectors.competitor_radar import CompetitorRadar
    from module2_cleaner import DataCleaner
    from urllib.parse import urlparse
    handled = 0
    for c in due:
        tid = c["tenant_id"]
        try:
            from api_platform import runtime_cfg, serper_keys_for, record_serper
            cfg = runtime_cfg(tid)        # 注入平台代付主 key（客户没填自己的时）
            if not cfg.get("deepseek_api_key"):
                # 没 Key 没法重扫，推后一周避免反复空转
                admin_db.upsert_competitor(
                    tid, c["url"], c["host"], c.get("name", ""),
                    intel={}, distributor_count=c.get("distributor_count", 0),
                    snapshot_hash=c.get("snapshot_hash", ""),
                    frequency_days=c.get("frequency_days", 7), monitor=True)
                continue
            s_keys, _, s_blocked, _ = serper_keys_for(tid, cfg)
            if s_blocked:        # 平台额度用完，本月不重扫，推后避免空转
                admin_db.upsert_competitor(
                    tid, c["url"], c["host"], c.get("name", ""),
                    intel={}, distributor_count=c.get("distributor_count", 0),
                    snapshot_hash=c.get("snapshot_hash", ""),
                    frequency_days=c.get("frequency_days", 7), monitor=True)
                continue
            eng = CompetitorRadar()
            eng.deepseek_key = cfg.get("deepseek_api_key", "")
            eng.serper_keys  = s_keys
            eng.product_name = cfg.get("product_name", "")
            eng.target_countries = cfg.get("target_countries", [])
            eng.proxy        = cfg.get("radar_proxy", "")
            _prof = cfg.get("product_profile") or {}
            eng.my_category  = _prof.get("category") or cfg.get("product_name", "")
            eng.product_i18n = _prof.get("keywords_i18n") or {}
            out = eng.run(urls=[c["url"]], auto_search=False,
                          want_intel=True, max_competitors=1)
            record_serper(tid, cfg, out.get("serper_calls", 0))
            dist = out.get("distributors", [])
            if dist:
                DataCleaner().run(dist, source="competitor_radar",
                                  db_path=tenant_ctx.get_db_path(tid))
            card = out.get("intel", [{}])[0] if out.get("intel") else {}
            snap = _radar_snapshot(dist, card)
            dist_names = [f"{d['company_name']}({d.get('country') or '?'})"
                          for d in dist]
            admin_db.upsert_competitor(
                tid, c["url"], c["host"], c.get("name", ""),
                intel=card, distributor_count=len(dist), snapshot_hash=snap,
                frequency_days=c.get("frequency_days", 7), monitor=True,
                distributor_names=dist_names)
            handled += 1
            print(f"[radar] 已重扫监控竞品 {c['host']} → 经销商 {len(dist)} 家")
        except Exception as e:
            print(f"[radar] 监控 {c.get('host','?')} 异常: {e}")
    return handled


_radar_started = False


def start_radar_scheduler(interval: int = 3600) -> None:
    """每小时扫一次到点的监控竞品（实际重扫频率由每个竞品的 next_check_at 决定）。"""
    global _radar_started
    if _radar_started:
        return
    _radar_started = True

    def _loop():
        while True:
            time.sleep(interval)
            try:
                process_due_competitors()
            except Exception as e:
                print(f"[radar] 调度异常: {e}")

    threading.Thread(target=_loop, daemon=True).start()
    print("[radar] 竞品监控调度已启动")


start_radar_scheduler()


# ─────────────────────────────────────────────────────────
# 启动
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    admin_db.init()
    app.run(host="127.0.0.1", port=5001, debug=True, use_reloader=False)
