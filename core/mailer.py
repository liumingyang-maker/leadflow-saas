"""
core/mailer.py — 系统邮件发送工具（注册验证 / 密码重置）
读取 config.SMTP_*（来自 .env / 环境变量）。配阿里云 DirectMail 时只改 .env。
"""
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

# 用项目统一 logger：生产 waitress 下 print 会被缓冲、项目日志里看不到，
# 改 logger 后发信成功/失败原因在「项目日志」可见——配 DirectMail 排错全靠它。
try:
    from compat import logger
except Exception:                                  # pragma: no cover
    import logging
    logger = logging.getLogger("leadflow")


def _smtp_send(to_email: str, subject: str, html_body: str) -> None:
    """实际发信，出错抛异常（调用方决定吞还是抛）。"""
    if not config.SMTP_USER or not config.SMTP_PASS:
        raise RuntimeError("SMTP_USER / SMTP_PASS 未配置（检查服务器 .env）")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{config.SMTP_FROM_NAME} <{config.SMTP_USER}>"
    msg["To"] = to_email
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    context = ssl.create_default_context()
    port = int(config.SMTP_PORT)
    # 465 = SSL；587/25 = STARTTLS（阿里云 DirectMail 两种都支持，常用 465）
    if port == 465:
        with smtplib.SMTP_SSL(config.SMTP_HOST, port, context=context, timeout=20) as s:
            s.login(config.SMTP_USER, config.SMTP_PASS)
            s.sendmail(config.SMTP_USER, to_email, msg.as_string())
    else:
        with smtplib.SMTP(config.SMTP_HOST, port, timeout=20) as s:
            s.ehlo()
            s.starttls(context=context)
            s.login(config.SMTP_USER, config.SMTP_PASS)
            s.sendmail(config.SMTP_USER, to_email, msg.as_string())


def send_email(to_email: str, subject: str, html_body: str) -> bool:
    try:
        _smtp_send(to_email, subject, html_body)
        logger.info(f"[mailer] 发送成功 → {to_email}（via {config.SMTP_HOST}:{config.SMTP_PORT} "
                    f"as {config.SMTP_USER}）")
        return True
    except Exception as e:
        logger.warning(f"[mailer] 发送失败 → {to_email}：{type(e).__name__}: {e} "
                       f"（host={config.SMTP_HOST}:{config.SMTP_PORT} user={config.SMTP_USER}）")
        return False


def smtp_diagnose(to_email: str) -> dict:
    """测试发信：返回当前 SMTP 配置 + 成功/失败原因，给后台「测试发信」按钮用。
    密码做掩码，不回显明文。"""
    info = {
        "host": config.SMTP_HOST,
        "port": config.SMTP_PORT,
        "user": config.SMTP_USER,
        "from_name": config.SMTP_FROM_NAME,
        "pass_set": bool(config.SMTP_PASS),
    }
    try:
        _smtp_send(to_email,
                   "【获客雷达】测试邮件",
                   "<p>这是一封测试邮件。能收到说明发信通道已打通 ✅</p>")
        logger.info(f"[mailer] 测试发信成功 → {to_email}")
        return {"ok": True, "detail": "发送成功，请去收件箱（含垃圾箱）查收", **info}
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        logger.warning(f"[mailer] 测试发信失败 → {to_email}：{detail}")
        return {"ok": False, "detail": detail, **info}


def send_verification_email(to_email: str, token: str) -> bool:
    url = f"{config.SITE_URL}/verify-email/{token}"
    html = f"""
    <div style="font-family:-apple-system,sans-serif;max-width:480px;margin:0 auto;
                background:#f8fafc;border-radius:12px;padding:40px">
      <div style="text-align:center;margin-bottom:28px">
        <span style="font-size:26px;font-weight:800;color:#2563eb">LeadFlow</span>
      </div>
      <h2 style="color:#111827;font-size:18px;margin-bottom:12px">验证您的邮箱</h2>
      <p style="color:#374151;line-height:1.6;margin-bottom:24px">
        感谢注册 LeadFlow！请点击下方按钮验证邮箱，完成后即可开始 14 天免费试用。
      </p>
      <div style="text-align:center;margin-bottom:24px">
        <a href="{url}" style="display:inline-block;padding:13px 32px;
           background:#2563eb;color:#fff;border-radius:8px;
           text-decoration:none;font-weight:600;font-size:15px">
          验证邮箱
        </a>
      </div>
      <p style="color:#9ca3af;font-size:12px;text-align:center">
        链接 30 分钟内有效。如非本人操作请忽略此邮件。
      </p>
    </div>
    """
    return send_email(to_email, "【LeadFlow】请验证您的邮箱地址", html)


def send_reset_email(to_email: str, token: str) -> bool:
    url = f"{config.SITE_URL}/reset-password/{token}"
    html = f"""
    <div style="font-family:-apple-system,sans-serif;max-width:480px;margin:0 auto;
                background:#f8fafc;border-radius:12px;padding:40px">
      <div style="text-align:center;margin-bottom:28px">
        <span style="font-size:26px;font-weight:800;color:#2563eb">LeadFlow</span>
      </div>
      <h2 style="color:#111827;font-size:18px;margin-bottom:12px">重置您的密码</h2>
      <p style="color:#374151;line-height:1.6;margin-bottom:24px">
        我们收到了您的密码重置请求，点击下方按钮设置新密码。
      </p>
      <div style="text-align:center;margin-bottom:24px">
        <a href="{url}" style="display:inline-block;padding:13px 32px;
           background:#2563eb;color:#fff;border-radius:8px;
           text-decoration:none;font-weight:600;font-size:15px">
          重置密码
        </a>
      </div>
      <p style="color:#9ca3af;font-size:12px;text-align:center">
        链接 30 分钟内有效。如非本人操作请忽略此邮件，密码不会被更改。
      </p>
    </div>
    """
    return send_email(to_email, "【LeadFlow】密码重置请求", html)
