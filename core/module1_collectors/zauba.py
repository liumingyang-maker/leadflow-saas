"""
module1_collectors/zauba.py — 印度海关数据采集
=================================================
数据来源：Zauba.com（印度进出口公开数据聚合平台）
  - 完全免费，无需注册或 API Key
  - 数据来自印度海关记录，真实可靠
  - 覆盖：印度（全球第二大摩托车市场）

工作逻辑：
  1. 优先从 Zauba.com 网页解析真实进口商
  2. 如遇 Cloudflare / 反爬限制，自动降级为模拟数据
  3. 采集到公司名后，配合 Hunter.io 可补充邮箱

注意：
  - 国内 IP 直连 Zauba 经常被 Cloudflare 拦截，建议代理或接受模拟数据
  - 模拟数据包含 15 家真实存在的印度摩托车行业城市及公司类型
"""
import time
import random
import re
import hashlib

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.zauba.com/",
}

ZAUBA_BASE = "https://www.zauba.com"

# 模拟数据：印度主要地区的摩托车/发动机进口商
# 格式：(公司名, 城市, 联系人职位, 近6月进口次数, 估计金额USD)
_MOCK_IMPORTERS = [
    ("Hero Auto Spares Trading Co",        "New Delhi",   "Import Manager",    24, 320000),
    ("Rajkot Engineering Importers LLP",   "Rajkot",      "Procurement Head",  18, 145000),
    ("Chennai Moto Parts Wholesale Co",    "Chennai",     "General Manager",   15,  98000),
    ("Ludhiana Engine Traders",            "Ludhiana",    "Director",          12,  75000),
    ("Mumbai Auto Components Hub",         "Mumbai",      "Purchasing Manager",10,  62000),
    ("Punjab Motor Works Pvt Ltd",         "Chandigarh",  "Import Director",    8,  48000),
    ("Coimbatore Machinery Importers",     "Coimbatore",  "Owner",              7,  35000),
    ("Ahmedabad Engine Supply Co",         "Ahmedabad",   "CEO",                6,  28000),
    ("Delhi Automotive Parts International","New Delhi",  "VP Procurement",    20, 195000),
    ("Hyderabad Motor Trading Corp",       "Hyderabad",   "Managing Director",  5,  21000),
    ("Jaipur Two Wheeler Parts Ltd",       "Jaipur",      "Operations Head",    9,  52000),
    ("Kanpur Auto Engines Distributors",   "Kanpur",      "Purchase Director", 11,  68000),
    ("Pune Machinery Import House",        "Pune",        "Import Head",       14,  89000),
    ("Surat Engine Wholesale Traders",     "Surat",       "Partner",            4,  18000),
    ("Kolkata Motor Parts Importers",      "Kolkata",     "Director Imports",   7,  38000),
]

# 公司名正则：匹配全大写的印度公司名（Zauba 页面上公司名格式）
_COMPANY_RE = re.compile(
    r'[>"\']([A-Z][A-Z0-9\s&.,\-\'()]{6,70}'
    r'(?:PVT\.?\s*LTD|LIMITED|LLP|INC|CORP|CO\.|TRADING|ENTERPRISES?'
    r'|INDUSTRIES|IMPORTS?|EXPORTS?|INTERNATIONAL|DISTRIBUTORS?|SUPPLIERS?))'
    r'[<"\'<]',
    re.IGNORECASE,
)


class ZaubaCollector:
    """
    印度海关进口商采集器。

    用法（仿照 apollo.py，在 app.py 的 run_bg 里直接设置属性）：
        zc = ZaubaCollector()
        zc.product_name    = cfg.get("product_name", "")
        zc.search_keywords = cfg.get("search_keywords", [])
        zc.hs_codes        = cfg.get("hs_codes", ["8407"])
        leads = zc.fetch_all()
    """

    def __init__(self):
        self.product_name    = ""
        self.search_keywords = []
        self.hs_codes        = []
        self._cache          = {}        # 内存缓存，同次任务不重复请求

    # ── 网页采集 ───────────────────────────────────────────────────────────

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(HEADERS)
        return s

    def _scrape_one_keyword(self, keyword: str, session: requests.Session) -> list[dict]:
        """
        向 Zauba 搜索页发请求，用正则从 HTML 中提取印度进口商公司名。

        Zauba 使用 Cloudflare；国内 IP 通常被拦截（返回 403/CAPTCHA）。
        遇到拦截时返回空列表，由调用方降级到模拟数据。
        """
        slug = keyword.strip().replace(" ", "+").replace("/", "-")
        url  = f"{ZAUBA_BASE}/import-{slug}-india.html"

        try:
            time.sleep(random.uniform(3.0, 6.0))   # 礼貌性延迟
            resp = session.get(url, timeout=30, allow_redirects=True)

            # 检测反爬屏障
            if resp.status_code in (403, 429, 503):
                print(f"[Zauba] HTTP {resp.status_code}，被限流")
                return []

            body = resp.text
            cf_signs = ("captcha", "cf-browser-verification",
                        "just a moment", "ddos-guard",
                        "security check", "please wait")
            if any(x in body[:5000].lower() for x in cf_signs):
                print("[Zauba] 检测到 Cloudflare 验证，跳过")
                return []

            if resp.status_code != 200:
                return []

            # 从 HTML 中提取大写公司名
            matches  = _COMPANY_RE.findall(body)
            seen     = set()
            leads    = []
            hs       = (self.hs_codes or ["8407"])[0]

            for raw_name in matches:
                name = raw_name.strip()
                key  = name.upper()
                if key in seen or len(name) < 8:
                    continue
                # 过滤导航/广告等无关词
                if any(x in key for x in ("ZAUBA", "CONTACT US", "PRIVACY",
                                           "TERMS", "LOGIN", "REGISTER")):
                    continue
                seen.add(key)
                leads.append({
                    "company_name": name.title(),
                    "country":      "India",
                    "hs_codes":     [hs],
                    "sources":      ["zauba"],
                    "notes":        f"Zauba印度海关 | {keyword}",
                })

            print(f"[Zauba] '{keyword}' → {len(leads)} 家")
            return leads[:25]

        except requests.RequestException as e:
            print(f"[Zauba] 网络错误: {e}")
            return []
        except Exception as e:
            print(f"[Zauba] 解析异常: {e}")
            return []

    # ── 模拟数据 ───────────────────────────────────────────────────────────

    def _mock_leads(self) -> list[dict]:
        """返回包含真实城市/职位信息的印度进口商模拟数据"""
        hs = (self.hs_codes or ["8407"])[0]
        return [
            {
                "company_name":        name,
                "country":             "India",
                "city":                city,
                "contact_title":       title,
                "hs_codes":            [hs],
                "import_count_6m":     cnt,
                "estimated_value_usd": val,
                "sources":             ["zauba"],
                "notes":               f"Zauba模拟数据 | HS:{hs}",
            }
            for name, city, title, cnt, val in _MOCK_IMPORTERS
        ]

    # ── 主入口 ─────────────────────────────────────────────────────────────

    def fetch_all(self, mock: bool = False) -> list[dict]:
        """
        采集印度进口商。

        参数：
          mock — True 时直接返回模拟数据（演示/测试用，不发网络请求）

        返回标准化 leads 列表，可直接传给 DataCleaner().run()
        """
        if mock:
            print("[Zauba] mock=True，返回模拟数据")
            return self._mock_leads()

        # 构建搜索词列表
        terms = []
        if self.product_name:
            terms.append(self.product_name)
        for kw in self.search_keywords:
            if kw not in terms:
                terms.append(kw)
        if not terms:
            terms = ["motorcycle engine"]

        session    = self._new_session()
        all_leads  = []
        seen_names = set()

        for term in terms[:2]:          # 最多搜2个词，控制请求量
            ck = "zauba_" + hashlib.md5(term.encode()).hexdigest()[:8]
            if ck in self._cache:
                batch = self._cache[ck]
            else:
                batch = self._scrape_one_keyword(term, session)
                self._cache[ck] = batch

            for lead in batch:
                key = lead["company_name"].upper()
                if key not in seen_names:
                    seen_names.add(key)
                    all_leads.append(lead)

        if all_leads:
            print(f"[Zauba] 真实采集完成：{len(all_leads)} 家印度进口商")
            return all_leads

        # 真实采集无结果（国内 IP 常被 Cloudflare 拦截）→ 返回空，绝不编造数据
        print("[Zauba] 真实采集无结果 → 返回空（不编造数据；印度海关需境外节点）")
        return []


# 单例（供直接 import 使用）
zauba_collector = ZaubaCollector()
