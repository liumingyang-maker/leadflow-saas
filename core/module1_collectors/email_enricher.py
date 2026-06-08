"""
module1_collectors/email_enricher.py — 一键找邮箱（theHarvester + Photon 融合实现）
====================================================================================
给一个公司官网/域名 → 自动找出邮箱、电话、社交账号。

融合了两个知名开源工具的核心思路：
  • theHarvester（GitHub ⭐16k+）：从搜索引擎里挖一个域名散落在各处的邮箱
  • Photon（GitHub ⭐12k+）：爬公司官网各页面，正则抠出邮箱/电话/社交链接

这两个工具本体是重型命令行程序（依赖多、安装难、部分需境外网络），这里用
requests/curl_cffi + 正则 + 你已有的 Serper、Hunter Key 原生实现同一能力，
零额外依赖、即装即用、境内可直接运行。

三路并行找联系方式，结果合并去重、按可信度排序：
  A. 官网爬取（Photon 式）：抓首页 + 常见联系页（/contact、/about…）
  B. 搜索引擎挖掘（theHarvester 式）：Serper 搜 "@域名" 找公开邮箱
  C. Hunter.io：按域名查公司决策人邮箱（配了 hunter_api_key 时）

用法（在 app.py 路由里）：
    en = EmailEnricher()
    en.serper_key = cfg.get("serpapi_key", "")
    en.hunter_key = cfg.get("hunter_api_key", "")
    result = en.enrich(website=lead["website"], company_name=lead["company_name"])
    # result["best_email"] / result["emails"] / result["phones"] / result["socials"]
"""

import re
import time
import random
from typing import Optional
from urllib.parse import urljoin

import requests

try:
    from curl_cffi import requests as cf
    _HAS_CF = True
except Exception:                                  # pragma: no cover
    _HAS_CF = False

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/120.0.0.0 Safari/537.36")

# ── 正则 ────────────────────────────────────────────────────────────────────
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)
# 国际电话：可选 +，7~15 位数字，允许空格/横线/括号
_PHONE_RE = re.compile(
    r"(?<![\w.])(\+?\d[\d\s().\-]{7,16}\d)(?![\w])"
)
_SOCIAL_RE = {
    "linkedin": re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:company|in)/[A-Za-z0-9_\-%./]+", re.I),
    "facebook": re.compile(r"https?://(?:www\.)?facebook\.com/[A-Za-z0-9_.\-/]+", re.I),
    "whatsapp": re.compile(r"https?://(?:wa\.me|api\.whatsapp\.com)/[A-Za-z0-9_?=&+./]+", re.I),
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/[A-Za-z0-9_.\-/]+", re.I),
    "twitter": re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/[A-Za-z0-9_./]+", re.I),
}

# 邮箱黑名单：图片/示例/第三方资源/占位等，不是真实联系邮箱
_EMAIL_JUNK_DOMAINS = {
    "example.com", "example.org", "domain.com", "email.com", "yourcompany.com",
    "sentry.io", "sentry-next.wixpress.com", "wix.com", "wixpress.com",
    "schema.org", "w3.org", "googleapis.com", "gstatic.com", "cloudflare.com",
    "fontawesome.com", "jquery.com", "bootstrapcdn.com",
}
_EMAIL_JUNK_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
                   ".css", ".js", ".ico", ".woff", ".woff2")

# 联系页常见路径（Photon 式定向爬取，命中率高）
_CONTACT_PATHS = ["", "/contact", "/contact-us", "/contactus", "/contact.html",
                  "/about", "/about-us", "/aboutus", "/about.html",
                  "/company", "/imprint", "/impressum", "/support",
                  "/en/contact", "/contacts"]


class EmailEnricher:

    def __init__(self):
        self.serper_key = ""
        self.hunter_key = ""
        self.timeout    = 18
        self.max_pages  = 5          # 官网最多爬几个页面

    # ── 工具 ────────────────────────────────────────────────────────────────

    @staticmethod
    def extract_domain(url_or_email: str) -> Optional[str]:
        """从 URL 或邮箱里取根域名。abc@x.co.uk / https://www.x.co.uk/a → x.co.uk"""
        if not url_or_email:
            return None
        s = url_or_email.strip().lower()
        if "@" in s and "/" not in s:
            s = s.split("@")[-1]
        s = re.sub(r"^https?://", "", s)
        s = re.sub(r"^www\.", "", s)
        domain = s.split("/")[0].split("?")[0].split(":")[0].strip()
        return domain if "." in domain else None

    def _http_get(self, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        if _HAS_CF:
            try:
                r = cf.get(url, impersonate="chrome120", timeout=self.timeout,
                           allow_redirects=True)
                if r.status_code == 200 and r.text:
                    return r.text
            except Exception:
                pass
        try:
            r = requests.get(url, headers={"User-Agent": _UA,
                                           "Accept-Language": "en-US,en;q=0.9"},
                             timeout=self.timeout, allow_redirects=True)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass
        return ""

    # ── 从文本里扫联系方式 ───────────────────────────────────────────────────

    def _scan(self, text: str, domain: str) -> dict:
        """从一段 HTML/文本里抠出邮箱、电话、社交链接。"""
        out = {"emails": set(), "phones": set(), "socials": {}}
        if not text:
            return out

        # 邮箱：处理常见混淆（[at] (at) 等）
        deob = (text.replace("[at]", "@").replace("(at)", "@").replace(" at ", "@")
                    .replace("[dot]", ".").replace("(dot)", "."))
        for m in _EMAIL_RE.findall(deob):
            em = m.strip().strip(".").lower()
            edom = em.split("@")[-1]
            if edom in _EMAIL_JUNK_DOMAINS:
                continue
            if em.endswith(_EMAIL_JUNK_EXT):
                continue
            if len(em) > 60 or em.count("@") != 1:
                continue
            out["emails"].add(em)

        # 电话
        for m in _PHONE_RE.findall(text):
            digits = re.sub(r"\D", "", m)
            if 8 <= len(digits) <= 15:
                out["phones"].add(m.strip())

        # 社交
        for name, rgx in _SOCIAL_RE.items():
            hit = rgx.search(text)
            if hit and name not in out["socials"]:
                out["socials"][name] = hit.group(0)

        return out

    @staticmethod
    def _merge(acc: dict, part: dict) -> None:
        acc["emails"].update(part["emails"])
        acc["phones"].update(part["phones"])
        for k, v in part["socials"].items():
            acc["socials"].setdefault(k, v)

    # ── A. 官网爬取（Photon 式）─────────────────────────────────────────────

    def _crawl_site(self, website: str, domain: str, acc: dict) -> None:
        base = website if website.startswith("http") else "http://" + website
        pages_done = 0
        for path in _CONTACT_PATHS:
            if pages_done >= self.max_pages:
                break
            url = urljoin(base, path) if path else base
            html = self._http_get(url)
            if not html:
                continue
            pages_done += 1
            self._merge(acc, self._scan(html, domain))
            time.sleep(random.uniform(0.2, 0.5))
            # 首页找到邮箱后，联系页再多看一两个就够了
            if acc["emails"] and pages_done >= 3:
                break

    # ── B. 搜索引擎挖掘（theHarvester 式）───────────────────────────────────

    def _serper_dork(self, domain: str, company: str, acc: dict) -> None:
        if not self.serper_key:
            return
        queries = [f'"@{domain}"', f'{company} email contact', f'site:{domain} email']
        for q in queries:
            try:
                resp = requests.post(
                    "https://google.serper.dev/search",
                    headers={"X-API-KEY": self.serper_key,
                             "Content-Type": "application/json"},
                    json={"q": q, "num": 10}, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                blob = " ".join(
                    (it.get("title", "") + " " + it.get("snippet", "") + " " + it.get("link", ""))
                    for it in data.get("organic", [])
                )
                part = self._scan(blob, domain)
                # 搜索结果里只信本域名邮箱，避免引入无关第三方邮箱
                part["emails"] = {e for e in part["emails"]
                                  if e.split("@")[-1].endswith(domain)}
                self._merge(acc, part)
                time.sleep(random.uniform(0.4, 0.9))
            except Exception as e:
                print(f"[EmailEnricher] Serper dork 失败 ({q}): {e}")

    # ── C. Hunter.io ────────────────────────────────────────────────────────

    def _hunter(self, domain: str) -> list[dict]:
        if not self.hunter_key:
            return []
        try:
            resp = requests.get(
                "https://api.hunter.io/v2/domain-search",
                params={"domain": domain, "api_key": self.hunter_key, "limit": 10},
                timeout=15)
            resp.raise_for_status()
            emails = resp.json().get("data", {}).get("emails", [])
            out = []
            for e in emails:
                if not e.get("value"):
                    continue
                out.append({
                    "email": e["value"].lower(),
                    "name": f"{e.get('first_name','')} {e.get('last_name','')}".strip(),
                    "title": e.get("position", "") or "",
                    "confidence": int(e.get("confidence", 0)),
                    "source": "hunter",
                })
            return out
        except Exception as e:
            print(f"[EmailEnricher] Hunter.io 失败: {e}")
            return []

    # ── 邮箱可信度评分（排序用）─────────────────────────────────────────────

    @staticmethod
    def _email_score(email: str, source: str, title: str = "") -> int:
        score = {"hunter": 70, "website": 60, "serper": 45}.get(source, 40)
        local = email.split("@")[0]
        # 采购/决策相关前缀加分
        if any(k in local for k in ("purchas", "procure", "import",
                                    "buyer", "sourcing", "trade", "sales")):
            score += 15
        if any(k in (title or "").lower() for k in ("purchas", "procure",
                                                    "import", "buyer", "director", "manager")):
            score += 10
        # info/contact 是通用邮箱，略减
        if local in ("info", "contact", "admin", "office", "mail", "enquiry", "enquiries"):
            score -= 5
        # 个人姓名邮箱（含点，像 john.doe）通常更直达决策人
        if "." in local and local not in ("info", "contact"):
            score += 5
        return max(0, min(100, score))

    # ── 主入口 ──────────────────────────────────────────────────────────────

    def enrich(self, website: str = "", company_name: str = "",
               domain: str = "") -> dict:
        """
        返回：
          {
            "ok": bool, "domain": str,
            "emails": [ {email,name,title,confidence,source} ... ]  # 已按可信度降序
            "phones": [str ...], "socials": {linkedin/facebook/...},
            "best_email": str, "summary": str
          }
        """
        domain = domain or self.extract_domain(website) or self.extract_domain(company_name)
        if not domain:
            return {"ok": False, "domain": "", "emails": [], "phones": [],
                    "socials": {}, "best_email": "",
                    "summary": "没有可用的域名/官网，无法查找。请先补充该客户的官网。"}

        acc = {"emails": set(), "phones": set(), "socials": {}}

        # A. 官网爬取
        if website:
            try:
                self._crawl_site(website, domain, acc)
            except Exception as e:
                print(f"[EmailEnricher] 官网爬取异常: {e}")

        # B. 搜索引擎挖掘
        try:
            self._serper_dork(domain, company_name or domain, acc)
        except Exception as e:
            print(f"[EmailEnricher] 搜索挖掘异常: {e}")

        # 汇总邮箱并标注来源（website / serper）
        email_records: dict[str, dict] = {}
        for em in acc["emails"]:
            src = "website" if em.split("@")[-1].endswith(domain) else "serper"
            email_records[em] = {"email": em, "name": "", "title": "",
                                 "source": src, "confidence": 0}

        # C. Hunter.io（覆盖/补充，带姓名职位与官方置信度）
        for h in self._hunter(domain):
            rec = email_records.get(h["email"])
            if rec:
                rec.update({"name": h["name"] or rec["name"],
                            "title": h["title"] or rec["title"],
                            "source": "hunter",
                            "confidence": h["confidence"]})
            else:
                email_records[h["email"]] = h

        # 计算最终可信度并排序
        emails = list(email_records.values())
        for r in emails:
            base = r.get("confidence", 0)
            calc = self._email_score(r["email"], r["source"], r.get("title", ""))
            r["confidence"] = max(base, calc) if r["source"] == "hunter" else calc
        emails.sort(key=lambda r: r["confidence"], reverse=True)

        phones = sorted(acc["phones"], key=len, reverse=True)[:5]
        best_email = emails[0]["email"] if emails else ""

        parts = []
        if emails:
            parts.append(f"{len(emails)} 个邮箱")
        if phones:
            parts.append(f"{len(phones)} 个电话")
        if acc["socials"]:
            parts.append(f"{len(acc['socials'])} 个社交账号")
        summary = ("找到 " + "、".join(parts)) if parts else \
                  "没有公开找到联系方式（该公司官网可能没有公开邮箱，可尝试 LinkedIn 深度调查）"

        return {
            "ok": bool(emails or phones or acc["socials"]),
            "domain": domain,
            "emails": emails,
            "phones": phones,
            "socials": acc["socials"],
            "best_email": best_email,
            "summary": summary,
        }


# 单例
email_enricher = EmailEnricher()
