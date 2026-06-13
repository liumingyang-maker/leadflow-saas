"""
module1_collectors/alibaba_rfq.py — 阿里巴巴 RFQ 采购需求采集
================================================================
数据来源：Alibaba.com 国际站 RFQ（Request for Quotation）
  - 完全公开，无需账号或 API Key
  - 数据：买家国家、产品需求描述、采购数量、发布时间
  - 覆盖：全球买家（非洲、东南亚、南亚买家发布最多）

工作逻辑：
  1. 搜索与产品相关的 RFQ 列表
  2. 解析买家国家、需求描述、采购量
  3. 公司名从需求描述推断或标注为"RFQ买家"
  4. 遇反爬限制降级为模拟数据

说明：
  - RFQ 买家都有明确采购意向，质量高于普通线索
  - 公司名通常隐藏，需后续通过回复 RFQ 获取联系方式
  - 本模块采集后主要用于了解市场需求 + 锁定目标国家

真实抓取（crawlee 等价方案）：
  阿里 RFQ 页面是 JS 渲染的，普通 requests 拿不到数据。crawlee-python 靠内置浏览器
  解决，但浏览器内核在 Windows 便携版 Python 上安装沉重、且阿里反爬对国内 IP 很凶。
  这里改用 curl_cffi 伪装 Chrome TLS 指纹抓取 + DeepSeek 智能解析（ai_extractor）的
  组合：能抓到内容时就用 AI 从乱七八糟的 HTML 里提结构化 RFQ，抓不到则降级模拟数据。
  部署到境外服务器后，真实抓取成功率会明显提升。
"""
import time
import random
import re
import hashlib

import requests

try:
    from curl_cffi import requests as cf
    _HAS_CF = True
except Exception:                                  # pragma: no cover
    _HAS_CF = False

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.alibaba.com/",
}

ALIBABA_RFQ_URL = "https://sourcing.alibaba.com/rfq/rfq_search_list.htm"

# 阿里巴巴 RFQ 模拟数据
# 格式：(买家国家, 产品描述, 采购数量, 单位, 联系人职位)
_MOCK_RFQS = [
    # 非洲
    ("Nigeria",       "CG150 motorcycle engine complete set, need 200 units quarterly",  200, "units",  "Procurement Officer"),
    ("Kenya",         "4-stroke petrol engine 110cc, looking for reliable supplier",      500, "pcs",   "Import Manager"),
    ("Tanzania",      "Tricycle engine 200cc, need quotation urgently",                  100, "sets",  "Owner"),
    ("Ghana",         "Motorcycle spare parts engine assembly CG125",                    300, "pcs",   "Director"),
    ("Ethiopia",      "Small gasoline engine for generator 5HP-7HP",                    150, "units",  "Purchasing"),
    # 东南亚
    ("Vietnam",       "CB engine 125cc OHC, need 1000pcs per month, regular order",    1000, "pcs",   "Import Head"),
    ("Indonesia",     "Motorcycle engine 150cc complete, PT company looking for OEM",    500, "sets",  "Sourcing Manager"),
    ("Philippines",   "Small engine 110cc for motorcycle, need CE certificate",          200, "pcs",   "GM"),
    ("Cambodia",      "Engine assembly for motorbike, 100cc-125cc range",                80,  "sets",  "Director"),
    # 南亚
    ("Pakistan",      "CG125 engine parts, looking for competitive price from China",   2000, "pcs",   "Purchase Manager"),
    ("Bangladesh",    "100cc motorcycle engine complete set, MOQ 500pcs",               500, "units",  "Import Director"),
    ("Sri Lanka",     "Engine for bajaj style 3-wheeler, need samples first",            50,  "sets",  "Owner"),
    # 拉美
    ("Brazil",        "Motor completo para moto 150cc Honda style, cotação urgente",    300, "units",  "Comprador"),
    ("Mexico",        "Complete motorcycle engine 150cc, need Mexican NOM certificate", 500, "pcs",   "Gerente Compras"),
    ("Colombia",      "Motor de moto 125cc completo, precio CIF Bogota",               200, "sets",  "Director"),
    # 中东
    ("UAE",           "Motorcycle engine 125-150cc, Dubai trader looking for supplier", 1000, "pcs",   "Trading Manager"),
    ("Saudi Arabia",  "Engine assembly for scooter 150cc, halal certificate needed",    300, "units",  "Procurement"),
]


class AlibabaRFQCollector:
    """
    阿里巴巴 RFQ 采购需求采集器。

    用法：
        rc = AlibabaRFQCollector()
        rc.product_name    = cfg.get("product_name", "")
        rc.search_keywords = cfg.get("search_keywords", [])
        leads = rc.fetch_all()
    """

    def __init__(self):
        self.product_name    = ""
        self.search_keywords = []
        self.deepseek_key    = ""        # 配了则启用 AI 智能解析（crawlee 等价路径）
        self._cache          = {}

    # ── 网页采集 ─────────────────────────────────────────────────────────

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(HEADERS)
        return s

    def _scrape_rfq_smart(self, keyword: str) -> list[dict]:
        """
        crawlee 等价路径：curl_cffi 伪装 Chrome 抓页 + DeepSeek 从 HTML 提结构化 RFQ。
        抓不到内容或没配 deepseek_key 时返回空，由调用方降级。
        """
        if not (_HAS_CF and self.deepseek_key):
            return []
        url = f"{ALIBABA_RFQ_URL}?keywords={keyword.replace(' ', '+')}&period=90days"
        try:
            time.sleep(random.uniform(2.0, 4.0))
            r = cf.get(url, impersonate="chrome120", timeout=30, allow_redirects=True)
            body = r.text or ""
            if r.status_code != 200 or len(body) < 1500:
                return []
            if any(x in body[:3000].lower()
                   for x in ("captcha", "verify", "robot", "blocked")):
                print("[AlibabaRFQ] curl_cffi 仍被反爬拦截")
                return []

            from module1_collectors.ai_extractor import AIExtractor
            ex = AIExtractor()
            ex.deepseek_key = self.deepseek_key
            text = ex.html_to_text(body, max_chars=7000)
            rows = ex.extract_from_text(
                text,
                instruction=("这是阿里巴巴 RFQ（采购需求）搜索页。提取每条采购需求，"
                             "字段：country（买家国家，英文）、title（需求描述）、"
                             "quantity（采购数量，没有就留空）。只要真实采购需求。"),
            )
            leads, seen = [], set()
            for row in rows:
                country = str(row.get("country", "")).strip()
                title   = str(row.get("title", "")).strip()
                if not country or not title or country in seen:
                    continue
                seen.add(country)
                qty = str(row.get("quantity", "")).strip()
                note = f"[RFQ需求] {title}" + (f" | 采购量：{qty}" if qty else "")
                leads.append({
                    "company_name": f"RFQ买家·{country}",
                    "country":      country,
                    "notes":        note[:200],
                    "sources":      ["alibaba_rfq"],
                    "status":       "new",
                })
            if leads:
                print(f"[AlibabaRFQ] AI 智能解析 '{keyword}' → {len(leads)} 条RFQ")
            return leads[:20]
        except Exception as e:
            print(f"[AlibabaRFQ] 智能抓取异常: {e}")
            return []

    def _scrape_rfq(self, keyword: str, session: requests.Session) -> list[dict]:
        """
        从阿里巴巴 RFQ 搜索页抓取采购需求。
        阿里巴巴反爬较强，主要通过 JS 渲染，直接请求可能只能拿到部分数据。
        """
        params = {
            "keywords": keyword,
            "period":   "90days",       # 近90天的需求
        }

        try:
            time.sleep(random.uniform(2.0, 4.0))
            resp = session.get(ALIBABA_RFQ_URL, params=params, timeout=25)

            if resp.status_code in (403, 429, 503):
                print(f"[AlibabaRFQ] HTTP {resp.status_code}，被限流")
                return []

            body = resp.text
            if any(x in body[:3000].lower()
                   for x in ("captcha", "verify", "robot", "blocked")):
                print("[AlibabaRFQ] 检测到反爬验证")
                return []

            if resp.status_code != 200 or len(body) < 1000:
                return []

            leads  = []
            seen   = set()

            # 从 JSON 数据块提取（阿里巴巴页面常嵌入 JSON）
            json_pat  = re.compile(r'"country"\s*:\s*"([A-Za-z\s]{2,40})".*?"title"\s*:\s*"([^"]{10,200})"', re.DOTALL)
            # 从 HTML 提取国家 + 描述
            html_pat  = re.compile(
                r'class="[^"]*country[^"]*"[^>]*>\s*([A-Za-z\s]{2,30})\s*<.*?'
                r'class="[^"]*title[^"]*"[^>]*>\s*([^<]{10,200})\s*<',
                re.DOTALL
            )

            for pat in (json_pat, html_pat):
                for country, title in pat.findall(body):
                    country = country.strip()
                    title   = title.strip()
                    if not country or not title or country in seen:
                        continue
                    seen.add(country)
                    leads.append({
                        "company_name": f"RFQ买家·{country}",
                        "country":      country,
                        "notes":        title[:200],
                        "sources":      ["alibaba_rfq"],
                        "status":       "new",
                    })

            print(f"[AlibabaRFQ] '{keyword}' → {len(leads)} 条RFQ")
            return leads[:20]

        except requests.RequestException as e:
            print(f"[AlibabaRFQ] 网络错误: {e}")
            return []
        except Exception as e:
            print(f"[AlibabaRFQ] 解析异常: {e}")
            return []

    # ── 模拟数据 ─────────────────────────────────────────────────────────

    def _mock_leads(self) -> list[dict]:
        leads = []
        for country, desc, qty, unit, title in _MOCK_RFQS:
            leads.append({
                "company_name":  f"RFQ买家·{country}",
                "country":       country,
                "contact_title": title,
                "notes":         f"[RFQ需求] {desc} | 采购量：{qty} {unit}",
                "sources":       ["alibaba_rfq"],
                "status":        "new",
            })
        return leads

    # ── 主入口 ────────────────────────────────────────────────────────────

    def fetch_all(self, mock: bool = False) -> list[dict]:
        """
        采集 RFQ 采购需求。
        mock=True 时返回模拟数据（覆盖非洲/东南亚/南亚/拉美等重点市场）。
        """
        if mock:
            print("[AlibabaRFQ] mock=True，返回模拟数据")
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
        seen_keys  = set()

        for term in terms[:2]:
            ck = "rfq_" + hashlib.md5(term.encode()).hexdigest()[:8]
            if ck in self._cache:
                batch = self._cache[ck]
            else:
                # 先走 AI 智能抓取（crawlee 等价），失败再退正则抓取
                batch = self._scrape_rfq_smart(term) or self._scrape_rfq(term, session)
                self._cache[ck] = batch

            for lead in batch:
                key = lead["company_name"] + lead["country"]
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_leads.append(lead)

        if all_leads:
            print(f"[AlibabaRFQ] 真实采集完成：{len(all_leads)} 条RFQ需求")
            return all_leads

        # 真实采集无结果 → 返回空，绝不编造数据
        print("[AlibabaRFQ] 真实采集无结果 → 返回空（不编造数据；JS渲染页常需境外节点）")
        return []


alibaba_rfq_collector = AlibabaRFQCollector()
