"""
core/whatsapp_sender.py — WhatsApp 官方 API 发送（A 方案，预留接口）
=====================================================================
WhatsApp 触达有两条路：
  • B 方案（已上线）：详情页生成 wa.me 点击链接，人工跳转手动发。零成本、零封号、马上能用。
  • A 方案（本文件）：通过官方 Business API 自动发送，能进系统、能群发模板消息。
    需要 Meta 企业认证 + 服务商账号 + 独立号码，按条/按会话付费。

本文件把 A 方案的发送接口预留好，支持三家主流服务商，客户在设置里配好 Key 即可启用：
  1. Twilio            —— 最常用，全球可用
  2. 360dialog         —— 官方 BSP，欧洲/新兴市场常用
  3. Meta Cloud API    —— Meta 官方直连，免 BSP

没配置时 is_configured() 返回 False，调用方据此回退到 B 方案，不影响现有使用。

cfg 字段：
  wa_provider      = twilio | 360dialog | cloud
  # Twilio
  wa_account_sid, wa_auth_token, wa_from   (wa_from 是已认证的 WhatsApp 发送号，带国家码)
  # 360dialog
  wa_api_key
  # Meta Cloud API
  wa_phone_id, wa_token

用法：
    from whatsapp_sender import WhatsAppSender
    w = WhatsAppSender(cfg)
    if w.is_configured():
        ok, info = w.send("+2348011112222", "Hello from ...")
"""

import re
import requests


def _clean_number(num: str) -> str:
    """规范成 E.164 风格的纯数字（带国家码，不含 + 与符号）。"""
    return re.sub(r"[^0-9]", "", num or "")


class WhatsAppSender:

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.provider = (self.cfg.get("wa_provider") or "").lower()

    def provider_label(self) -> str:
        return {"twilio": "Twilio", "360dialog": "360dialog",
                "cloud": "Meta Cloud API"}.get(self.provider, "未选择")

    def is_configured(self) -> bool:
        c = self.cfg
        if self.provider == "twilio":
            return bool(c.get("wa_account_sid") and c.get("wa_auth_token") and c.get("wa_from"))
        if self.provider == "360dialog":
            return bool(c.get("wa_api_key"))
        if self.provider == "cloud":
            return bool(c.get("wa_phone_id") and c.get("wa_token"))
        return False

    def send(self, to_number: str, text: str) -> tuple[bool, str]:
        to = _clean_number(to_number)
        if not to:
            return False, "号码为空或无效"
        if not text:
            return False, "消息内容为空"
        if not self.is_configured():
            return False, f"WhatsApp 官方 API（{self.provider_label()}）未配置完整"
        try:
            if self.provider == "twilio":
                return self._send_twilio(to, text)
            if self.provider == "360dialog":
                return self._send_360(to, text)
            if self.provider == "cloud":
                return self._send_cloud(to, text)
            return False, f"未知服务商：{self.provider}"
        except Exception as e:
            return False, f"发送异常：{e}"

    # ── Twilio ──────────────────────────────────────────────────────────────
    def _send_twilio(self, to: str, text: str) -> tuple[bool, str]:
        sid   = self.cfg["wa_account_sid"].strip()
        token = self.cfg["wa_auth_token"].strip()
        frm   = _clean_number(self.cfg["wa_from"])
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            auth=(sid, token),
            data={"From": f"whatsapp:+{frm}", "To": f"whatsapp:+{to}", "Body": text},
            timeout=25)
        if r.status_code in (200, 201):
            return True, "已通过 Twilio 发送"
        return False, f"Twilio 返回 {r.status_code}：{r.text[:200]}"

    # ── 360dialog ───────────────────────────────────────────────────────────
    def _send_360(self, to: str, text: str) -> tuple[bool, str]:
        key = self.cfg["wa_api_key"].strip()
        r = requests.post(
            "https://waba.360dialog.io/v1/messages",
            headers={"D360-API-KEY": key, "Content-Type": "application/json"},
            json={"to": to, "type": "text", "text": {"body": text}},
            timeout=25)
        if r.status_code in (200, 201):
            return True, "已通过 360dialog 发送"
        return False, f"360dialog 返回 {r.status_code}：{r.text[:200]}"

    # ── Meta Cloud API ──────────────────────────────────────────────────────
    def _send_cloud(self, to: str, text: str) -> tuple[bool, str]:
        phone_id = self.cfg["wa_phone_id"].strip()
        token    = self.cfg["wa_token"].strip()
        r = requests.post(
            f"https://graph.facebook.com/v19.0/{phone_id}/messages",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"messaging_product": "whatsapp", "to": to,
                  "type": "text", "text": {"body": text}},
            timeout=25)
        if r.status_code in (200, 201):
            return True, "已通过 Meta Cloud API 发送"
        return False, f"Cloud API 返回 {r.status_code}：{r.text[:200]}"


def make_whatsapp_sender(cfg: dict) -> WhatsAppSender:
    return WhatsAppSender(cfg)
