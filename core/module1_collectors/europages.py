"""
module1_collectors/europages.py — 欧洲 B2B 目录采集
=======================================================
数据来源：Europages.com（欧洲最大 B2B 目录，280万企业）
  - 完全免费，无需注册或 API Key
  - 覆盖：德国、法国、意大利、西班牙、荷兰等29个欧洲国家
  - 数据：公司名、国家、城市、网站、电话

工作逻辑：
  1. 优先从 Europages.com 搜索页解析真实进口商
  2. 如遇反爬限制，自动降级为模拟数据
  3. 采集到公司名 + 网站后，配合 Hunter.io 可补充邮箱

注意：
  - Europages 对爬虫相对友好（公开目录），但仍有频率限制
  - 建议搜索词用英文，覆盖效果更好
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
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": "https://www.europages.com/",
}

EUROPAGES_BASE = "https://www.europages.com"

# 模拟数据：欧洲各国的摩托车/机械进口商
# 格式：(公司名, 国家, 城市, 网站, 电话前缀)
_MOCK_IMPORTERS = [
    # 德国
    ("Mueller Motorentechnik GmbH",         "Germany",     "Munich",       "mueller-motorentechnik.de",  "+49 89"),
    ("Rheinland Engine Trading KG",          "Germany",     "Cologne",      "rheinland-engines.de",       "+49 221"),
    ("Berlin Auto Parts Import GmbH",        "Germany",     "Berlin",       "berlin-autoparts.de",        "+49 30"),
    # 法国
    ("Importation Moteurs France SARL",      "France",      "Lyon",         "importation-moteurs.fr",     "+33 4"),
    ("Paris Moto Distribution SA",           "France",      "Paris",        "parismoto-dist.fr",          "+33 1"),
    # 意大利
    ("Motori Importazioni Italia SRL",       "Italy",       "Milan",        "motoriimportazioni.it",      "+39 02"),
    ("Roma Engine Trade SpA",                "Italy",       "Rome",         "romaenginetrade.it",         "+39 06"),
    # 西班牙
    ("Importaciones Motor España SL",        "Spain",       "Barcelona",    "importacionesmotor.es",      "+34 93"),
    ("Madrid Componentes Moto SA",           "Spain",       "Madrid",       "madridcomponentes.es",       "+34 91"),
    # 荷兰
    ("Netherlands Engine Import BV",         "Netherlands", "Rotterdam",    "nl-engine-import.nl",        "+31 10"),
    ("Amsterdam Parts Trading BV",           "Netherlands", "Amsterdam",    "amstparts.nl",               "+31 20"),
    # 波兰
    ("Polska Import Silniki Sp.zo.o",        "Poland",      "Warsaw",       "polska-silniki.pl",          "+48 22"),
    ("Krakow Motorparts Trading",            "Poland",      "Krakow",       "krakow-motorparts.pl",       "+48 12"),
    # 比利时
    ("Belgian Engine Distributors NV",       "Belgium",     "Antwerp",      "belgian-engines.be",         "+32 3"),
    # 葡萄牙
    ("Importação Motores Portugal Lda",      "Portugal",    "Lisbon",       "importacaomotores.pt",       "+351 21"),
]

# 从HTML提取公司名的正则（Europages 用特定 CSS 类）
_NAME_PATTERNS = [
    re.compile(r'class="[^"]*company-name[^"]*"[^>]*>\s*<[^>]+>([^<]{3,80})</'),
    re.compile(r'data-company-name="([^"]{3,80})"'),
    re.compile(r'"name"\s*:\s*"([^"]{3,80})"'),
    re.compile(r'<h2[^>]*class="[^"]*card[^"]*"[^>]*>\s*([^<]{5,80})\s*</h2>'),
]

_COUNTRY_PATTERN = re.compile(
    r'class="[^"]*country[^"]*"[^>]*>\s*([A-Za-z\s]{3,30})\s*<',
    re.IGNORECASE
)

_WEBSITE_PATTERN = re.compile(
    r'href="(https?://(?!(?:www\.)?europages)[^\s"]{8,100})"[^>]*class="[^"]*website',
    re.IGNORECASE
)


class EuropagesCollector:
    """
    欧洲 B2B 目录进口商采集器。

    用法（与 zauba.py 完全一致）：
        ec = EuropagesCollector()
        ec.product_name    = cfg.get("product_name", "")
        ec.search_keywords = cfg.get("search_keywords", [])
        leads = ec.fetch_all()
    """

    def __init__(self):
        self.product_name    = ""
        self.search_keywords = []
        self._cache          = {}

    # ── 网页采集 ─────────────────────────────────────────────────────────

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(HEADERS)
        return s

    def _scrape_keyword(self, keyword: str, session: requests.Session) -> list[dict]:
        """
        搜索 Europages，解析公司列表。
        Europages 相对友好，但也有请求频率限制。
        """
        slug = keyword.strip().lower().replace(" ", "-").replace("/", "-")
        url  = f"{EUROPAGES_BASE}/companies/{slug}.html"

        try:
            time.sleep(random.uniform(2.0, 4.0))
            resp = session.get(url, timeout=25, allow_redirects=True)

            if resp.status_code in (403, 429, 503):
                print(f"[Europages] HTTP {resp.status_code}，被限流")
                return []

            body = resp.text
            if any(x in body[:3000].lower()
                   for x in ("captcha", "cf-browser-verification",
                              "just a moment", "ddos-guard")):
                print("[Europages] 检测到反爬验证")
                return []

            if resp.status_code != 200:
                return []

            leads   = []
            seen    = set()

            # 尝试多种正则提取公司名
            names = []
            for pat in _NAME_PATTERNS:
                names = pat.findall(body)
                if len(names) >= 3:
                    break

            # 提取网站
            websites = _WEBSITE_PATTERN.findall(body)

            for i, raw_name in enumerate(names[:20]):
                name = raw_name.strip()
                if not name or name.upper() in seen or len(name) < 4:
                    continue
                if any(x in name.lower() for x in ("europages", "cookie", "privacy")):
                    continue
                seen.add(name.upper())

                leads.append({
                    "company_name": name,
                    "country":      "Europe",        # 后续标准化时按实际国家覆盖
                    "website":      websites[i] if i < len(websites) else "",
                    "hs_codes":     [],
                    "sources":      ["europages"],
                    "notes":        f"Europages | {keyword}",
                })

            print(f"[Europages] '{keyword}' → {len(leads)} 家")
            return leads

        except requests.RequestException as e:
            print(f"[Europages] 网络错误: {e}")
            return []
        except Exception as e:
            print(f"[Europages] 解析异常: {e}")
            return []

    # ── 模拟数据 ─────────────────────────────────────────────────────────

    def _mock_leads(self) -> list[dict]:
        return [
            {
                "company_name": name,
                "country":      country,
                "city":         city,
                "website":      f"https://www.{website}",
                "phone":        phone,
                "sources":      ["europages"],
                "notes":        "Europages模拟数据",
            }
            for name, country, city, website, phone in _MOCK_IMPORTERS
        ]

    # ── 主入口 ────────────────────────────────────────────────────────────

    def fetch_all(self, mock: bool = False) -> list[dict]:
        """
        采集欧洲进口商。
        mock=True 时返回 15 条模拟数据（演示/测试用）。
        """
        if mock:
            print("[Europages] mock=True，返回模拟数据")
            return self._mock_leads()

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

        for term in terms[:2]:
            ck = "ep_" + hashlib.md5(term.encode()).hexdigest()[:8]
            if ck in self._cache:
                batch = self._cache[ck]
            else:
                batch = self._scrape_keyword(term, session)
                self._cache[ck] = batch

            for lead in batch:
                key = lead["company_name"].upper()
                if key not in seen_names:
                    seen_names.add(key)
                    all_leads.append(lead)

        if all_leads:
            print(f"[Europages] 真实采集完成：{len(all_leads)} 家欧洲进口商")
            return all_leads

        # 真实采集无结果 → 返回空，绝不编造数据
        print("[Europages] 真实采集无结果 → 返回空（不编造数据；如被反爬请用境外节点）")
        return []


europages_collector = EuropagesCollector()
