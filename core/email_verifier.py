"""
core/email_verifier.py — 邮箱真伪验证
=========================================
发开发信之前，先判断一个邮箱「是不是真实存在、能不能收信」，过滤掉死邮箱。
往一堆无效邮箱群发会被判定为垃圾发件人、拉低整个发信域名信誉 —— 这是保护送达率的关键一步。

四层检查（从快到慢、从确定到尽力而为）：
  1. 语法格式：正则校验，明显写错的直接毙掉
  2. 一次性/垃圾域名：过滤 mailinator 这类临时邮箱
  3. MX 记录：用 DNS-over-HTTPS（Google/Cloudflare 的 JSON 接口）查该域名有没有收信服务器
     —— 不依赖 dnspython，零额外依赖，境内外都能查
  4. SMTP 探测（尽力而为）：连到对方 MX 服务器，走 MAIL FROM/RCPT TO 看是否接受该地址
     —— 注意：很多云服务器/ISP 封了出站 25 端口，探测超时算「未知」而非「无效」，
        不会误杀。部署到放开 25 端口的服务器后，这一层判断会更准。

结论分级：
  valid    邮箱真实可收信（MX有 + SMTP接受）           → 放心发
  risky    域名能收信，但无法逐个确认（MX有，SMTP未知）→ 可发，注意退信
  invalid  语法错误 / 域名根本没有收信服务器            → 别发
  disposable 一次性临时邮箱                              → 别发
  unknown  网络问题查不动                                → 谨慎

用法：
    from email_verifier import EmailVerifier
    r = EmailVerifier().verify("purchase@abcmotors.ng")
    if r["can_send"]: ...
    # 批量： EmailVerifier().verify_many([...])
"""

import re
import socket
import smtplib
import requests

_SYNTAX_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})$")

# 常见一次性/临时邮箱域名（节选，命中即判 disposable）
_DISPOSABLE = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "temp-mail.org", "throwawaymail.com", "yopmail.com", "getnada.com",
    "trashmail.com", "sharklasers.com", "maildrop.cc", "fakeinbox.com",
    "dispostable.com", "mailnesia.com", "mintemail.com", "spam4.me",
}

_DOH_ENDPOINTS = [
    "https://dns.google/resolve",
    "https://cloudflare-dns.com/dns-query",
]


class EmailVerifier:

    def __init__(self):
        self.smtp_probe = True          # 是否做 SMTP 探测（端口被封时自动降级）
        self.smtp_timeout = 8
        self.from_addr = "verify@leadflow.app"   # 探测时用的发件地址（不真正发信）
        self._mx_cache: dict = {}

    # ── MX 查询（DNS-over-HTTPS）─────────────────────────────────────────────

    def _lookup_mx(self, domain: str) -> list[str]:
        """返回该域名的 MX 主机列表（按优先级排序）。查不到返回 []。"""
        if domain in self._mx_cache:
            return self._mx_cache[domain]
        hosts: list[tuple[int, str]] = []
        for ep in _DOH_ENDPOINTS:
            try:
                r = requests.get(ep, params={"name": domain, "type": "MX"},
                                 headers={"accept": "application/dns-json"}, timeout=8)
                if r.status_code != 200:
                    continue
                for ans in r.json().get("Answer", []):
                    if ans.get("type") != 15:       # 15 = MX
                        continue
                    parts = ans.get("data", "").split()
                    if len(parts) == 2:
                        pref = int(parts[0]) if parts[0].isdigit() else 50
                        host = parts[1].rstrip(".")
                        hosts.append((pref, host))
                    elif len(parts) == 1:
                        hosts.append((50, parts[0].rstrip(".")))
                if hosts:
                    break
            except Exception:
                continue
        result = [h for _, h in sorted(hosts)]
        self._mx_cache[domain] = result
        return result

    # ── SMTP 探测（尽力而为）─────────────────────────────────────────────────

    def _probe_smtp(self, mx_hosts: list[str], email: str):
        """
        连到 MX 服务器走 RCPT TO 探测。
        返回 True=接受 / False=明确拒绝 / None=无法确定（超时/端口被封/灰名单）
        """
        for host in mx_hosts[:2]:
            try:
                server = smtplib.SMTP(timeout=self.smtp_timeout)
                server.connect(host, 25)
                server.helo("leadflow.app")
                server.mail(self.from_addr)
                code, _ = server.rcpt(email)
                try:
                    server.quit()
                except Exception:
                    pass
                if code in (250, 251):
                    return True
                if code in (550, 551, 553, 554):   # 明确不存在/拒收
                    return False
                return None                          # 灰名单等，无法确定
            except (socket.timeout, ConnectionRefusedError, OSError):
                # 出站 25 被封 / 连不上 → 无法确定，换下一个或放弃
                continue
            except smtplib.SMTPException:
                return None
        return None

    # ── 主入口 ──────────────────────────────────────────────────────────────

    def verify(self, email: str) -> dict:
        email = (email or "").strip().lower()
        out = {"email": email, "status": "invalid", "reason": "",
               "mx": [], "can_send": False}

        # 1. 语法
        m = _SYNTAX_RE.match(email)
        if not m:
            out["reason"] = "邮箱格式不正确"
            return out
        domain = m.group(1)

        # 2. 一次性域名
        if domain in _DISPOSABLE:
            out["status"] = "disposable"
            out["reason"] = "一次性临时邮箱，不要发送"
            return out

        # 3. MX
        mx = self._lookup_mx(domain)
        out["mx"] = mx
        if not mx:
            out["reason"] = f"域名 {domain} 没有收信服务器（MX），邮箱很可能无效"
            return out

        # 4. SMTP 探测
        probe = self._probe_smtp(mx, email) if self.smtp_probe else None
        if probe is True:
            out.update({"status": "valid", "can_send": True,
                        "reason": "邮箱真实存在，可放心发送"})
        elif probe is False:
            out.update({"status": "invalid", "can_send": False,
                        "reason": "对方服务器明确表示该地址不存在"})
        else:
            out.update({"status": "risky", "can_send": True,
                        "reason": "域名能收信，但无法逐个确认（可发送，留意退信）"})
        return out

    def verify_many(self, emails: list[str]) -> list[dict]:
        seen, results = set(), []
        for e in emails:
            key = (e or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            results.append(self.verify(key))
        return results


# 单例
email_verifier = EmailVerifier()
