"""
module1_collectors/osint_investigator.py — 高价值客户深度调查（spiderfoot + LinkedIn）
========================================================================================
对一个重点客户做「一键背调」，把零散信息聚合成一份调查报告，帮你判断：
这家公司值不值得重点跟、该找谁、靠不靠谱。

灵感来自两个开源项目：
  • SpiderFoot（GitHub ⭐14k+）：OSINT 自动化，把一个目标的公开情报全网聚合
  • linkedin_scraper（GitHub ⭐4k+）：找公司里的决策人

⚠️ 关于 LinkedIn：直接爬 LinkedIn 需要登录账号、违反其 ToS、且极易封号/封 IP，
   不适合放进对外销售的 SaaS。这里改用「合规等价方案」——通过 Serper 搜索
   `site:linkedin.com/in 公司名 采购/采购经理`，拿到决策人的公开档案链接和职位，
   既安全又能直达对的人，不需要任何 LinkedIn 账号。

报告聚合四部分：
  A. 真实性体检（调用 company_verifier）
  B. 联系方式（调用 email_enricher：邮箱/电话/社交）
  C. LinkedIn 决策人（Serper 搜公开档案）
  D. 公开提及 / 新闻（Serper 搜公司名，看这家公司在网上的活跃度）

用法：
    inv = OSINTInvestigator()
    inv.serper_key = cfg.get("serpapi_key", "")
    inv.hunter_key = cfg.get("hunter_api_key", "")
    report = inv.investigate(company_name=lead["company_name"],
                             website=lead["website"], country=lead["country"])
"""

import time
import random
import requests

from module1_collectors.email_enricher import EmailEnricher
from module1_collectors.company_verifier import CompanyVerifier

# 采购/决策相关职位关键词（找对的人）
_DECISION_KEYWORDS = ("purchasing OR procurement OR import OR buyer "
                      "OR sourcing OR \"supply chain\" OR director OR CEO OR owner")


class OSINTInvestigator:

    def __init__(self):
        self.serper_key = ""
        self.hunter_key = ""
        self.enricher = EmailEnricher()
        self.verifier = CompanyVerifier()

    # ── C. LinkedIn 决策人（合规：Serper 搜公开档案）─────────────────────────

    def _find_linkedin_people(self, company: str, country: str = "") -> list[dict]:
        if not self.serper_key:
            return []
        q = f'site:linkedin.com/in "{company}" ({_DECISION_KEYWORDS})'
        if country:
            q += f" {country}"
        people = []
        try:
            resp = requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": self.serper_key, "Content-Type": "application/json"},
                json={"q": q, "num": 10}, timeout=15)
            resp.raise_for_status()
            for it in resp.json().get("organic", []):
                link = it.get("link", "")
                if "linkedin.com/in/" not in link:
                    continue
                title = it.get("title", "")
                # LinkedIn 标题格式通常是 "姓名 - 职位 - 公司 | LinkedIn"
                parts = [p.strip() for p in title.replace("|", " - ").split(" - ")]
                name = parts[0] if parts else title
                role = parts[1] if len(parts) > 1 else ""
                people.append({
                    "name": name[:60],
                    "role": role[:80],
                    "linkedin_url": link,
                    "snippet": it.get("snippet", "")[:140],
                })
        except Exception as e:
            print(f"[OSINT] LinkedIn 搜索失败: {e}")
        return people[:6]

    # ── D. 公开提及 / 活跃度 ────────────────────────────────────────────────

    def _find_mentions(self, company: str) -> list[dict]:
        if not self.serper_key:
            return []
        out = []
        try:
            resp = requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": self.serper_key, "Content-Type": "application/json"},
                json={"q": f'"{company}"', "num": 10}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            for it in data.get("organic", [])[:6]:
                out.append({
                    "title": it.get("title", "")[:100],
                    "url": it.get("link", ""),
                    "snippet": it.get("snippet", "")[:140],
                })
        except Exception as e:
            print(f"[OSINT] 公开提及搜索失败: {e}")
        return out

    # ── 主入口 ──────────────────────────────────────────────────────────────

    def investigate(self, company_name: str, website: str = "",
                    country: str = "") -> dict:
        report = {"company_name": company_name, "country": country,
                  "website": website}

        # A. 真实性体检
        if website:
            try:
                report["verification"] = self.verifier.verify(website=website)
            except Exception as e:
                print(f"[OSINT] 验证异常: {e}")
                report["verification"] = None
        else:
            report["verification"] = None

        # B. 联系方式
        try:
            self.enricher.serper_key = self.serper_key
            self.enricher.hunter_key = self.hunter_key
            report["contacts"] = self.enricher.enrich(
                website=website, company_name=company_name)
        except Exception as e:
            print(f"[OSINT] 联系方式异常: {e}")
            report["contacts"] = None
        time.sleep(random.uniform(0.3, 0.7))

        # C. LinkedIn 决策人
        report["people"] = self._find_linkedin_people(company_name, country)
        time.sleep(random.uniform(0.3, 0.7))

        # D. 公开提及
        report["mentions"] = self._find_mentions(company_name)

        # 综合一句话结论
        v = report.get("verification") or {}
        contacts = report.get("contacts") or {}
        bits = []
        if v.get("score") is not None:
            bits.append(f"真实性 {v['score']}/100")
        if contacts.get("emails"):
            bits.append(f"{len(contacts['emails'])} 个邮箱")
        if report["people"]:
            bits.append(f"{len(report['people'])} 位 LinkedIn 决策人")
        if report["mentions"]:
            bits.append(f"{len(report['mentions'])} 条网络提及")
        report["summary"] = "调查完成：" + ("、".join(bits) if bits else "公开信息较少")
        return report


# 单例
osint_investigator = OSINTInvestigator()
