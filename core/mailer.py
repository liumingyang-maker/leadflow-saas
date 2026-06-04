"""
core/mailer.py — 邮件发送工具
"""
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config


def send_email(to_email: str, subject: str, html_body: str) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{config.SMTP_FROM_NAME} <{config.SMTP_USER}>"
        msg["To"] = to_email
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=context) as s:
            s.login(config.SMTP_USER, config.SMTP_PASS)
            s.sendmail(config.SMTP_USER, to_email, msg.as_string())
        return True
    except Exception as e:
        print(f"[mailer] 发送失败 → {e}")
        return False


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
