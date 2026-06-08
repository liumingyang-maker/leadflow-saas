"""
core/tenant_mailer.py — 双通道租户发信
=========================================
给每个租户发「外贸开发信」用。客户在设置里二选一：

  通道 A · 专业 ESP（推荐认真做邮件营销）
    SendGrid / Mailgun 这类专业发信服务，REST API 发送。
    优点：送达率高、自带打开/点击追踪、能稳定群发。
    缺点：按量付费、要配独立域名 + DKIM。
    （注：阿里云邮件推送的客户可走「通道 B」并把 SMTP 服务器填 smtpdm.aliyun.com）

  通道 B · 邮箱 SMTP（起步/小量）
    QQ 企业邮箱 / Gmail / 阿里云邮件推送 等，标准 SMTP 发送。
    优点：免费、配置简单、和现有邮箱一致。
    缺点：群发易进垃圾箱、海外送达率一般、无追踪。

⚠️ 注意：这里发的是「客户给海外买家的开发信」，用租户自己配的发信账号；
   和 core/mailer.py（平台自己发的注册验证/密码重置系统邮件）是两回事，互不影响。

内建追踪钩子：send() 接收可选的 tracking={"open_url":..., "click_base":...}，
传入时会在 HTML 里注入追踪像素 + 改写链接，供「邮件打开追踪」功能直接对接。
不传则正常发送，不影响现在使用。

用法：
    from tenant_mailer import TenantMailer
    m = TenantMailer(cfg)                 # cfg = 租户 config.json
    ok, info = m.send(to_email, subject, body_text)
    print(m.channel_label())              # 给界面显示当前用哪个通道
"""

import re
import ssl
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr

import requests


def _text_to_html(text: str) -> str:
    """把纯文本开发信转成简单 HTML（保留换行），刻意保持朴素以利送达率。"""
    esc = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    body = esc.replace("\n", "<br>")
    return (f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
            f'line-height:1.6;color:#222">{body}</div>')


class TenantMailer:

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.channel = (self.cfg.get("mail_channel") or "smtp").lower()

    # ── 给界面用：当前通道说明 ──────────────────────────────────────────────

    def channel_label(self) -> str:
        if self.channel == "esp":
            prov = (self.cfg.get("esp_provider") or "sendgrid").capitalize()
            return f"专业ESP · {prov}"
        return "邮箱SMTP"

    def is_configured(self) -> bool:
        if self.channel == "esp":
            return bool(self.cfg.get("esp_api_key") and self.cfg.get("esp_from_email"))
        return bool(self.cfg.get("smtp_host") and self.cfg.get("smtp_user")
                    and self.cfg.get("smtp_pass"))

    # ── 发信主入口 ──────────────────────────────────────────────────────────

    def send(self, to_email: str, subject: str, body_text: str,
             to_name: str = "", tracking: dict = None) -> tuple[bool, str]:
        if not to_email:
            return False, "收件人邮箱为空"
        if not self.is_configured():
            return False, f"当前发信通道（{self.channel_label()}）未配置完整，请到设置页填写"

        html = _text_to_html(body_text)
        if tracking:
            html = self._inject_tracking(html, tracking)

        try:
            if self.channel == "esp":
                return self._send_esp(to_email, to_name, subject, body_text, html)
            return self._send_smtp(to_email, to_name, subject, body_text, html)
        except Exception as e:
            return False, f"发送异常：{e}"

    # ── 通道 B：SMTP ────────────────────────────────────────────────────────

    def _send_smtp(self, to_email, to_name, subject, text, html) -> tuple[bool, str]:
        host = self.cfg.get("smtp_host", "").strip()
        port = int(self.cfg.get("smtp_port") or 465)
        user = self.cfg.get("smtp_user", "").strip()
        pwd  = self.cfg.get("smtp_pass", "").strip()
        from_name  = self.cfg.get("smtp_from_name") or self.cfg.get("sender_name") \
            or self.cfg.get("company_name") or user
        from_email = self.cfg.get("smtp_from_email", "").strip() or user

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = formataddr((from_name, from_email))
        msg["To"] = formataddr((to_name, to_email)) if to_name else to_email
        msg.attach(MIMEText(text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))

        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as s:
                s.login(user, pwd)
                s.sendmail(from_email, [to_email], msg.as_string())
        else:   # 587 / STARTTLS
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                s.starttls(context=context)
                s.login(user, pwd)
                s.sendmail(from_email, [to_email], msg.as_string())
        return True, "已通过 SMTP 发送"

    # ── 通道 A：ESP ─────────────────────────────────────────────────────────

    def _send_esp(self, to_email, to_name, subject, text, html) -> tuple[bool, str]:
        prov = (self.cfg.get("esp_provider") or "sendgrid").lower()
        if prov == "sendgrid":
            return self._send_sendgrid(to_email, to_name, subject, text, html)
        if prov == "mailgun":
            return self._send_mailgun(to_email, to_name, subject, text, html)
        return False, f"未知的 ESP 服务商：{prov}"

    def _send_sendgrid(self, to_email, to_name, subject, text, html) -> tuple[bool, str]:
        key = self.cfg.get("esp_api_key", "").strip()
        from_email = self.cfg.get("esp_from_email", "").strip()
        from_name  = self.cfg.get("esp_from_name") or self.cfg.get("company_name") or from_email
        payload = {
            "personalizations": [{"to": [{"email": to_email,
                                          **({"name": to_name} if to_name else {})}]}],
            "from": {"email": from_email, "name": from_name},
            "subject": subject,
            "content": [{"type": "text/plain", "value": text},
                        {"type": "text/html", "value": html}],
            # SendGrid 原生打开/点击追踪
            "tracking_settings": {"click_tracking": {"enable": True},
                                  "open_tracking": {"enable": True}},
        }
        r = requests.post("https://api.sendgrid.com/v3/mail/send",
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"},
                          json=payload, timeout=25)
        if r.status_code in (200, 201, 202):
            return True, "已通过 SendGrid 发送"
        return False, f"SendGrid 返回 {r.status_code}：{r.text[:200]}"

    def _send_mailgun(self, to_email, to_name, subject, text, html) -> tuple[bool, str]:
        key    = self.cfg.get("esp_api_key", "").strip()
        domain = self.cfg.get("esp_domain", "").strip()
        region = (self.cfg.get("esp_region") or "us").lower()
        from_email = self.cfg.get("esp_from_email", "").strip()
        from_name  = self.cfg.get("esp_from_name") or self.cfg.get("company_name") or from_email
        if not domain:
            return False, "Mailgun 需要填写发信域名（esp_domain）"
        base = "https://api.eu.mailgun.net" if region == "eu" else "https://api.mailgun.net"
        to_field = formataddr((to_name, to_email)) if to_name else to_email
        r = requests.post(
            f"{base}/v3/{domain}/messages",
            auth=("api", key),
            data={"from": formataddr((from_name, from_email)),
                  "to": to_field, "subject": subject, "text": text, "html": html,
                  "o:tracking": "yes", "o:tracking-opens": "yes",
                  "o:tracking-clicks": "yes"},
            timeout=25)
        if r.status_code in (200, 201):
            return True, "已通过 Mailgun 发送"
        return False, f"Mailgun 返回 {r.status_code}：{r.text[:200]}"

    # ── 追踪注入钩子（供「邮件打开追踪」功能对接）───────────────────────────

    @staticmethod
    def _inject_tracking(html: str, tracking: dict) -> str:
        """
        tracking = {"open_url": 追踪像素URL, "click_base": 点击跳转前缀}
        在 HTML 末尾插入 1x1 像素；把 <a href> 改写成经追踪前缀的跳转链接。
        ESP（SendGrid/Mailgun）自带追踪时通常不需要这个；SMTP 通道才靠它。
        """
        open_url = tracking.get("open_url")
        click_base = tracking.get("click_base")
        if click_base:
            def _wrap(m):
                url = m.group(1)
                if url.startswith(("mailto:", "tel:", "#")):
                    return m.group(0)
                from urllib.parse import quote
                return f'href="{click_base}{quote(url, safe="")}"'
            html = re.sub(r'href="([^"]+)"', _wrap, html)
        if open_url:
            html += (f'<img src="{open_url}" width="1" height="1" '
                     f'style="display:none" alt="">')
        return html


def make_mailer(cfg: dict) -> TenantMailer:
    return TenantMailer(cfg)
