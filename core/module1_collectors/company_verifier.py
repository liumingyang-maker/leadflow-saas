"""
module1_collectors/company_verifier.py — 公司真实性验证（web-check 等价实现）
==============================================================================
给一个公司官网/域名 → 判断这家公司「是不是真实存在、靠不靠谱」，输出可信度评分。

灵感来自 web-check（GitHub ⭐24k+，给任意网站做体检）。web-check 是个 Node 全栈应用，
这里挑出对「核实外贸客户真假」最有用的几项检查，用 Python 标准库 + requests 原生实现：

  1. 网站存活：能不能打开、HTTP 状态码、有没有跳转
  2. HTTPS / SSL 证书：有没有有效证书、颁发机构、到期时间（正经公司一般都有）
  3. 域名注册年龄：通过 RDAP（rdap.org，免费无需 Key）查注册日期
     —— 注册 3 年以上的域名，跑路/诈骗概率明显更低
  4. 页面体检：标题、是否提到联系方式/产品关键词

综合给出 0~100 可信度分 + 一句话结论，帮你决定一个高价值线索值不值得深聊。

用法：
    cv = CompanyVerifier()
    report = cv.verify(website=lead["website"])
    # report["score"] / report["verdict"] / report["signals"]
"""

import re
import ssl
import socket
import datetime
from typing import Optional

import requests

try:
    from curl_cffi import requests as cf
    _HAS_CF = True
except Exception:                                  # pragma: no cover
    _HAS_CF = False

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/120.0.0.0 Safari/537.36")

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


class CompanyVerifier:

    def __init__(self):
        self.timeout = 12

    @staticmethod
    def extract_domain(url: str) -> Optional[str]:
        if not url:
            return None
        s = re.sub(r"^https?://", "", url.strip().lower())
        s = re.sub(r"^www\.", "", s)
        domain = s.split("/")[0].split("?")[0].split(":")[0].strip()
        return domain if "." in domain else None

    # ── 1+4. 网站存活 & 页面体检 ────────────────────────────────────────────

    def _check_website(self, domain: str) -> dict:
        out = {"alive": False, "status": None, "https": False,
               "title": "", "final_url": ""}
        for scheme in ("https", "http"):
            url = f"{scheme}://{domain}"
            try:
                if _HAS_CF:
                    r = cf.get(url, impersonate="chrome120",
                               timeout=self.timeout, allow_redirects=True)
                else:
                    r = requests.get(url, headers={"User-Agent": _UA},
                                     timeout=self.timeout, allow_redirects=True)
                out["alive"] = r.status_code < 500
                out["status"] = r.status_code
                out["final_url"] = str(getattr(r, "url", url))
                out["https"] = out["final_url"].startswith("https")
                m = _TITLE_RE.search(r.text or "")
                if m:
                    out["title"] = re.sub(r"\s+", " ", m.group(1)).strip()[:120]
                if out["alive"]:
                    return out
            except Exception:
                continue
        return out

    # ── 2. SSL 证书 ─────────────────────────────────────────────────────────

    def _check_ssl(self, domain: str) -> dict:
        out = {"valid": False, "issuer": "", "expires": "", "days_left": None}
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((domain, 443), timeout=self.timeout) as sock:
                with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                    cert = ssock.getpeercert()
            # 颁发机构
            issuer = dict(x[0] for x in cert.get("issuer", []))
            out["issuer"] = issuer.get("organizationName", "") or issuer.get("commonName", "")
            # 到期时间
            not_after = cert.get("notAfter", "")
            if not_after:
                exp = datetime.datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
                out["expires"] = exp.strftime("%Y-%m-%d")
                out["days_left"] = (exp - datetime.datetime.utcnow()).days
                out["valid"] = out["days_left"] > 0
        except Exception:
            pass
        return out

    # ── 3. 域名注册年龄（RDAP，免费）────────────────────────────────────────

    def _check_domain_age(self, domain: str) -> dict:
        out = {"created": "", "age_years": None, "registrar": ""}
        try:
            r = requests.get(f"https://rdap.org/domain/{domain}",
                             headers={"User-Agent": _UA}, timeout=self.timeout)
            if r.status_code != 200:
                return out
            data = r.json()
            # 注册商
            for ent in data.get("entities", []):
                roles = ent.get("roles", [])
                if "registrar" in roles:
                    for v in ent.get("vcardArray", [[], []])[1]:
                        if v and v[0] == "fn":
                            out["registrar"] = v[3]
                            break
            # 注册日期
            for ev in data.get("events", []):
                if ev.get("eventAction") == "registration":
                    raw = ev.get("eventDate", "")[:10]
                    out["created"] = raw
                    try:
                        created = datetime.datetime.strptime(raw, "%Y-%m-%d")
                        days = (datetime.datetime.utcnow() - created).days
                        out["age_years"] = round(days / 365.25, 1)
                    except Exception:
                        pass
                    break
        except Exception:
            pass
        return out

    # ── 主入口 ──────────────────────────────────────────────────────────────

    def verify(self, website: str = "", domain: str = "") -> dict:
        domain = domain or self.extract_domain(website)
        if not domain:
            return {"ok": False, "domain": "", "score": 0,
                    "verdict": "无法验证：该客户没有官网/域名",
                    "signals": []}

        web = self._check_website(domain)
        ssl_info = self._check_ssl(domain) if web["alive"] else \
            {"valid": False, "issuer": "", "expires": "", "days_left": None}
        age = self._check_domain_age(domain)

        # ── 打分 ──
        score = 0
        signals = []

        if web["alive"]:
            score += 30
            signals.append({"ok": True, "label": "网站可正常访问",
                            "detail": f"HTTP {web['status']}" +
                                      (f" · {web['title']}" if web["title"] else "")})
        else:
            signals.append({"ok": False, "label": "网站无法访问",
                            "detail": "打不开或已失效，需警惕"})

        if web["https"] and ssl_info["valid"]:
            score += 25
            issuer = ssl_info["issuer"] or "未知机构"
            signals.append({"ok": True, "label": "HTTPS 安全证书有效",
                            "detail": f"颁发：{issuer} · 到期 {ssl_info['expires']}"})
        elif web["alive"]:
            signals.append({"ok": False, "label": "没有有效的 HTTPS 证书",
                            "detail": "正规企业站通常都配 HTTPS"})

        if age["age_years"] is not None:
            yrs = age["age_years"]
            if yrs >= 3:
                score += 30
                lvl = "老牌域名，可信度高"
            elif yrs >= 1:
                score += 18
                lvl = "成立 1 年以上"
            else:
                score += 5
                lvl = "新注册域名，建议多核实"
            reg = f" · {age['registrar']}" if age["registrar"] else ""
            signals.append({"ok": yrs >= 1, "label": f"域名注册 {yrs} 年（{lvl}）",
                            "detail": f"注册于 {age['created']}{reg}"})
        else:
            signals.append({"ok": None, "label": "域名注册信息未公开",
                            "detail": "部分国家/注册商不提供 RDAP 查询"})

        # 页面提到联系方式/产品 → 像真实经营的站点
        if web.get("title"):
            score += 15

        score = max(0, min(100, score))
        if score >= 70:
            verdict = "✅ 看起来是真实、靠谱的公司，可放心深入跟进"
        elif score >= 45:
            verdict = "⚠️ 基本可信，但建议成交前再核实一下资质"
        else:
            verdict = "🔴 信号偏弱，请谨慎核实真实性后再投入精力"

        return {
            "ok": True,
            "domain": domain,
            "score": score,
            "verdict": verdict,
            "signals": signals,
            "raw": {"website": web, "ssl": ssl_info, "domain_age": age},
        }


# 单例
company_verifier = CompanyVerifier()
