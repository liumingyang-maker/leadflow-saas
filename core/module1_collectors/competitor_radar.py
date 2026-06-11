"""
module1_collectors/competitor_radar.py — 渠道雷达
====================================================
扒竞品官网的「经销商 / Distributors / Where to Buy / 代理商」页面，
用 AI 提取出一张渠道名单（公司/国家/电话/邮箱/网站），入库成自己的线索；
同时给每个竞品生成一张「竞品情报卡」（主营产品/目标市场/价格线索/动态）。

核心思路（复用已有模块，零额外依赖）：
  1. 找到竞品的经销商页：抓首页 HTML → 找含 dealer/distributor/where-to-buy
     /经销商/代理 等关键词的链接（找不到就直接拿首页 + 联系页兜底）。
  2. AI 提名单：把页面正文丢给 DeepSeek 结构化提取（复用 AIExtractor）。
  3. 情报卡：把首页/about 正文丢给 DeepSeek 提炼竞品概况。
  4. 自动搜竞品（方案②）：用 Serper 按产品关键词搜同行官网。

两种输入：
  - 方案①：用户手动填竞品网址（最准、零风险）
  - 方案②：给产品关键词，系统用 Serper 自动搜竞品官网

用法（在 app.py 的后台任务里设置属性后调用 run）：
    r = CompetitorRadar()
    r.deepseek_key = cfg.get("deepseek_api_key", "")
    r.serper_key   = cfg.get("serpapi_key", "")
    r.product_name = cfg.get("product_name", "")
    out = r.run(urls=["https://competitor.com"], auto_search=False, want_intel=True)
    # out = {"distributors": [lead dict...], "intel": [intel card...], "errors": [...]}
"""

import re
import time
import random
from urllib.parse import urljoin, urlparse

import requests

try:
    from ai_extractor import AIExtractor          # 同目录：抓网页 + DeepSeek 结构化
except Exception:                                  # pragma: no cover
    from module1_collectors.ai_extractor import AIExtractor

# ── 经销商页链接的关键词（链接文字或 href 里命中即认为是渠道页）──────────
_DEALER_HINTS = (
    "distributor", "distributator", "dealer", "where-to-buy", "where to buy",
    "wheretobuy", "stockist", "reseller", "retailer", "find-a-dealer",
    "partners", "our-partners", "agent", "sales-network", "global-network",
    "经销商", "代理商", "经销", "代理", "门店", "网点", "合作伙伴", "销售网络",
)
# 情报卡参考页（about / company）
_ABOUT_HINTS = ("about", "company", "profile", "who-we-are", "关于", "公司简介", "企业简介")

_A_HREF = re.compile(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                     re.IGNORECASE | re.DOTALL)
_TAGS   = re.compile(r"<[^>]+>")
_EMAIL  = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class CompetitorRadar:

    def __init__(self):
        self.deepseek_key = ""
        self.serper_key   = ""
        self.hunter_key   = ""
        self.product_name = ""
        self.search_keywords = []
        self.proxy        = ""           # 可选代理地址；空=走服务器本机 IP
        self.timeout      = 25
        self.max_dealer_pages = 3        # 每个竞品最多扒几个候选渠道页
        self.max_workers  = 4            # 多个不同竞品并行数（不同站=不同服务器，安全提速）
        self._ex          = AIExtractor()

    # ── 对外主入口 ───────────────────────────────────────────────────────────

    def run(self, urls=None, auto_search=False, want_intel=True,
            max_competitors=8) -> dict:
        """
        urls            — 手动填的竞品网址列表（方案①）
        auto_search     — True 时用 Serper 按产品关键词自动搜竞品（方案②）
        want_intel      — 是否生成竞品情报卡
        max_competitors — 本次最多处理几个竞品（控制时间/成本）
        """
        self._ex.deepseek_key = self.deepseek_key
        self._ex.proxy        = self.proxy        # 代理透传给底层抓取引擎
        sites = []
        seen  = set()

        for u in (urls or []):
            d = self._norm_site(u)
            if d and d not in seen:
                seen.add(d); sites.append(d)

        if auto_search:
            for d in self.search_competitors():
                if d not in seen:
                    seen.add(d); sites.append(d)

        sites = sites[:max_competitors]

        # 每个竞品用独立的 AIExtractor（各自的抓取缓存，线程安全），
        # 多个不同竞品并行跑（不同网站=不同服务器，不会搞崩任何一家）。
        def _process(site: str):
            ex = AIExtractor()
            ex.deepseek_key = self.deepseek_key
            ex.proxy        = self.proxy
            dist = self.scrape_distributors(site, ex=ex)
            card = self.build_intel(site, dist_count=len(dist), ex=ex) if want_intel else {}
            return site, dist, card

        all_dist, all_intel, errors = [], [], []
        if not sites:
            return {"distributors": all_dist, "intel": all_intel,
                    "errors": errors, "sites": sites}

        from concurrent.futures import ThreadPoolExecutor, as_completed
        workers = max(1, min(self.max_workers, len(sites)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_process, s): s for s in sites}
            for fut in as_completed(futs):
                site = futs[fut]
                try:
                    _s, dist, card = fut.result()
                    all_dist.extend(dist)
                    if card:
                        all_intel.append(card)
                except Exception as e:
                    errors.append(f"{site}: {e}")
                    print(f"[Radar] 处理 {site} 出错: {e}")

        return {"distributors": all_dist, "intel": all_intel,
                "errors": errors, "sites": sites}

    # ── 方案②：Serper 自动搜竞品官网 ────────────────────────────────────────

    def search_competitors(self, limit: int = 8) -> list:
        """按产品关键词用 Serper 搜同行官网，返回去重后的域名根 URL。"""
        if not self.serper_key:
            return []
        terms = []
        if self.product_name:
            terms.append(self.product_name)
        for kw in (self.search_keywords or [])[:2]:
            if kw and kw not in terms:
                terms.append(kw)
        if not terms:
            terms = ["motorcycle engine"]

        found, seen = [], set()
        # 加 supplier/manufacturer/factory 倾向，搜到的是同行卖家而非买家
        for term in terms[:2]:
            q = f"{term} manufacturer supplier OR factory OR distributor"
            try:
                resp = requests.post(
                    "https://google.serper.dev/search",
                    headers={"X-API-KEY": self.serper_key,
                             "Content-Type": "application/json"},
                    json={"q": q, "num": 10},
                    timeout=20,
                )
                resp.raise_for_status()
                for item in resp.json().get("organic", []):
                    link = item.get("link", "")
                    d = self._norm_site(link)
                    if not d:
                        continue
                    host = urlparse(d).netloc.lower()
                    # 跳过平台站（不是单个竞品官网）
                    if any(p in host for p in (
                        "alibaba", "made-in-china", "amazon", "ebay",
                        "facebook", "linkedin", "youtube", "wikipedia",
                        "indiamart", "globalsources", "tradeindia",
                        "google.", "blogspot", "pinterest")):
                        continue
                    if host not in seen:
                        seen.add(host); found.append(d)
                time.sleep(random.uniform(0.4, 0.9))
            except Exception as e:
                print(f"[Radar] Serper 搜竞品失败 '{term}': {e}")
        return found[:limit]

    # ── 找渠道页 + 提名单 ────────────────────────────────────────────────────

    def scrape_distributors(self, site: str, ex: AIExtractor = None) -> list:
        """扒一个竞品官网的经销商名单，返回标准 lead dict 列表。"""
        ex = ex or self._ex
        host = urlparse(site).netloc.replace("www.", "")
        home = ex.fetch_html(site)
        if not home:
            print(f"[Radar] 抓不到首页: {site}")
            return []

        pages = self._find_dealer_pages(site, home)
        if not pages:
            pages = [site]                      # 兜底：直接扫首页

        rows, seen = [], set()
        instruction = (
            "提取页面里所有经销商/代理商/分销商/门店/合作伙伴的信息。"
            "字段：company_name(公司或门店名), country(国家,英文), city(城市), "
            "phone(电话), email(邮箱), website(网站). "
            "只提取页面里真实出现的信息，没有的字段留空字符串，不要编造。"
            "如果是销售公司/经销商列表就提取，如果页面没有经销商名单就返回 []。"
        )
        for purl in pages[:self.max_dealer_pages]:
            try:
                data = ex.extract(purl, instruction, max_tokens=1500)
            except Exception as e:
                print(f"[Radar] 提取 {purl} 失败: {e}")
                continue
            for d in data:
                lead = self._to_lead(d, host, purl)
                if not lead:
                    continue
                key = (lead["company_name"].lower(), (lead.get("country") or "").lower())
                if key in seen:
                    continue
                seen.add(key)
                rows.append(lead)
            time.sleep(random.uniform(0.2, 0.5))   # 单站内部仍温柔，不轰
        print(f"[Radar] {host} → 渠道 {len(rows)} 家")
        return rows

    def _find_dealer_pages(self, base: str, home_html: str) -> list:
        """从首页 HTML 里找出指向经销商页的链接（绝对化 + 去重 + 限量）。"""
        cands, seen = [], set()
        for href, text in _A_HREF.findall(home_html):
            label = (_TAGS.sub(" ", text) + " " + href).lower()
            if any(h in label for h in _DEALER_HINTS):
                full = urljoin(base, href.strip())
                if not full.startswith(("http://", "https://")):
                    continue
                # 只跟本站链接，避免乱跳外站
                if urlparse(full).netloc.replace("www.", "") != \
                   urlparse(base).netloc.replace("www.", ""):
                    continue
                key = full.split("#")[0]
                if key not in seen:
                    seen.add(key); cands.append(key)
        return cands[:self.max_dealer_pages]

    # ── 竞品情报卡 ───────────────────────────────────────────────────────────

    def build_intel(self, site: str, dist_count: int = 0,
                    ex: AIExtractor = None) -> dict:
        """抓首页(+about)正文，AI 提炼竞品概况，返回情报卡 dict。"""
        ex = ex or self._ex
        host = urlparse(site).netloc.replace("www.", "")
        home = ex.fetch_html(site)
        if not home:
            return {}
        text = ex.html_to_text(home, max_chars=5000)

        # 找 about 页补充
        about = ""
        for href, label in _A_HREF.findall(home):
            l = (_TAGS.sub(" ", label) + " " + href).lower()
            if any(h in l for h in _ABOUT_HINTS):
                full = urljoin(site, href.strip())
                if full.startswith("http"):
                    ah = ex.fetch_html(full)
                    about = ex.html_to_text(ah, max_chars=3000)
                    break

        blob = (text + "\n\n" + about).strip()[:7000]
        instruction = (
            "下面是一家公司官网的正文。请总结这家公司的概况，只输出一个对象的 JSON 数组"
            "（数组里就一个对象），字段："
            "main_products(主营产品,中文一句话), "
            "target_markets(主攻市场/国家,中文), "
            "company_scale(规模线索,如成立年份/员工/产能,没有就留空), "
            "price_signal(价格或定位线索,如高端/性价比/MOQ,没有就留空), "
            "highlights(亮点或最新动态,中文一句话). 只依据正文,不要编造。"
        )
        cards = ex.extract_from_text(blob, instruction, max_tokens=600)
        card = cards[0] if cards else {}
        card.setdefault("main_products", "")
        card["competitor"]      = host
        card["url"]             = site
        card["distributor_count"] = dist_count
        return card

    # ── 工具 ─────────────────────────────────────────────────────────────────

    def _to_lead(self, d: dict, comp_host: str, src_url: str):
        """把 AI 提取的一条原始记录转成标准 lead dict；无公司名则丢弃。"""
        name = (d.get("company_name") or d.get("name") or "").strip()
        if not name or len(name) < 2:
            return None
        # 过滤明显不是公司名的噪音
        low = name.lower()
        if low in ("n/a", "none", "null", "-", "company_name", "distributor"):
            return None

        email = (d.get("email") or "").strip()
        if email and not _EMAIL.match(email):       # 只保留语法合法的真实邮箱
            email = ""
        phone = (d.get("phone") or d.get("tel") or "").strip()[:60]
        website = (d.get("website") or "").strip()
        if website and not website.startswith(("http://", "https://")):
            website = "http://" + website

        country = (d.get("country") or "").strip()[:60]
        city    = (d.get("city") or "").strip()[:60]

        notes = f"渠道雷达 | 竞品 {comp_host} 的经销商"
        if city:
            notes += f" | {city}"

        return {
            "company_name": name[:200],
            "country":      country or None,
            "city":         city or None,
            "website":      website or None,
            "email":        email or None,
            "phone":        phone or None,
            "sources":      ["competitor_radar"],
            "notes":        notes,
        }

    @staticmethod
    def _norm_site(u: str):
        """把用户输入/搜索结果归一化成 scheme://host 根地址。"""
        if not u:
            return None
        u = u.strip()
        if not u.startswith(("http://", "https://")):
            u = "https://" + u
        try:
            p = urlparse(u)
            if not p.netloc:
                return None
            return f"{p.scheme}://{p.netloc}"
        except Exception:
            return None


# 单例
competitor_radar = CompetitorRadar()
