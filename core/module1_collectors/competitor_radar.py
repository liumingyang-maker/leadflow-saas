"""
module1_collectors/competitor_radar.py — 渠道雷达（多源 + 质量闸版）
======================================================================
核心洞察：竞品的海外经销商 / 在卖竞品货的本地商家 / 进口竞品的买家，
         正是本租户（如摩托发动机出口商）最想拿下的客户。

四条数据源并行，不依赖竞品官网愿不愿意公开：
  ① 反向经销商搜索（reverse，主引擎）：拿竞品品牌名/型号全网搜 distributor/
     dealer/代理 + 目标国家（按国家语言本地化），搜出经销商自己的站再 AI 提取。
  ② 本地电商 + 社媒（social）：Facebook/Instagram/Jumia/MercadoLibre/IndiaMART。
  ③ 官网扫描（website）：扒竞品官网的 Distributors/Where-to-Buy 页。
  ④ 海关反查（customs）：Serper 搜公开海关聚合页里进口该品牌的买家。

七项强化（2026-06-11）：
  1. 质量闸：AI 给每条经销商打「匹配度 0-100 + 理由」。高分(>=q_auto)自动入库
     status=new，中分(>=q_review)入库 status=review（待确认区），低分直接丢。
  2. 本地化搜索：按目标国家语言切经销商关键词（distributeur/distribuidor/موزع）。
  3. 监控 diff：返回每竞品的经销商名单，供上层比对算「新增了谁」。
  4. 型号当搜索钥匙：情报卡提取主力型号，品牌名太泛(像城市)时降级用型号/产品词。
  5. 电话/WhatsApp 优先：提取偏向带区号手机号，入库后可直接 wa.me 触达。
  6. 大玩家置顶：同一经销商在多个竞品下出现 = 强信号，标「代理N个竞品」并加权置顶。
  7. 轻量验真 + 额度统计：可达性检查滤死站（capped）；统计本次 Serper 调用数。

实现复用 ai_extractor.py（抓页 + DeepSeek 结构化），零额外依赖。

用法（在 app.py 后台任务里设属性后调用 run）：
    r = CompetitorRadar()
    r.deepseek_key     = cfg.get("deepseek_api_key", "")
    r.serper_key       = cfg.get("serpapi_key", "")
    r.product_name     = cfg.get("product_name", "")
    r.target_countries = cfg.get("target_countries", [])
    r.sources          = ["reverse", "social", "website", "customs"]
    out = r.run(urls=["https://competitor.com"])
    # out = {"distributors":[...], "intel":[...], "errors":[...], "sites":[...],
    #        "serper_calls": int}
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

# 用项目统一 logger（生产环境项目日志可见；print 在 waitress 下被缓冲看不到）
try:
    from compat import logger
except Exception:                                  # pragma: no cover
    import logging
    logger = logging.getLogger("leadflow")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# ── 经销商页链接关键词 ────────────────────────────────────────────────────
_DEALER_HINTS = (
    "distributor", "distributator", "dealer", "where-to-buy", "where to buy",
    "wheretobuy", "stockist", "reseller", "retailer", "find-a-dealer",
    "partners", "our-partners", "agent", "sales-network", "global-network",
    "经销商", "代理商", "经销", "代理", "门店", "网点", "合作伙伴", "销售网络",
)
_ABOUT_HINTS = ("about", "company", "profile", "who-we-are", "关于", "公司简介", "企业简介")

# 平台/聚合/百科站：反向搜索时跳过（不是单个经销商的站）
_PLATFORM_HOSTS = (
    "alibaba", "made-in-china", "amazon", "ebay", "aliexpress", "dhgate",
    "1688.com", "taobao", "tmall", "jd.com", "globalsources", "ec21", "tradekey",
    "indiamart", "tradeindia", "wikipedia", "youtube", "google.", "blogspot",
    "pinterest", "quora", "reddit",
)

# 明显不是"竞品官网"的门户/平台/社交/搜索站：手动填进来要拦掉，别当竞品扫
_NOT_COMPETITOR = (
    # 门户/资讯
    "sina.", "sohu.", "163.com", "qq.com", "baidu.", "zhihu.", "csdn.",
    "bilibili.", "weibo.", "ifeng.", "people.com.cn", "xinhua", "cctv.",
    "toutiao.", "douban.",
    # 社交
    "instagram.", "facebook.", "twitter.", "linkedin.", "tiktok.",
    "youtube.", "pinterest.", "reddit.", "whatsapp.", "telegram.", "t.me",
    # 电商/B2B 平台
    "alibaba.", "1688.com", "taobao.", "tmall.", "jd.com", "pinduoduo.",
    "amazon.", "ebay.", "aliexpress.", "made-in-china.", "dhgate.",
    "indiamart.", "globalsources.", "tradeindia.", "ec21.", "tradekey.",
    # 搜索/百科
    "google.", "bing.com", "yahoo.", "wikipedia.", "baike.",
)


def is_invalid_competitor_url(url: str) -> bool:
    """判断一个手动填的网址是不是『明显不是竞品官网』（门户/平台/社交/搜索）。"""
    if not url:
        return True
    u = url.strip()
    if not u.startswith(("http://", "https://")):
        u = "http://" + u
    try:
        host = urlparse(u).netloc.lower().replace("www.", "")
    except Exception:
        return True
    if not host:
        return True
    return any(tok in host for tok in _NOT_COMPETITOR)


# 四源中文标签（写进线索 notes）
_VIA = {
    "reverse": "反向搜索", "social": "电商社媒",
    "website": "官网经销商", "customs": "海关反查",
}

# 各语言的「经销商/代理」同义词，用于本地化搜索
_LANG_TERMS = {
    "en": ["distributor", "dealer", "wholesaler", "importer", "reseller"],
    "fr": ["distributeur", "revendeur", "grossiste", "importateur"],
    "es": ["distribuidor", "mayorista", "importador", "revendedor"],
    "pt": ["distribuidor", "importador", "atacadista", "revendedor"],
    "ar": ["موزع", "تاجر", "مستورد"],
}

# 目标国家（中文/英文名）→ (英文名, 语言)。覆盖用户主攻的非洲/拉美/中东/南亚/东南亚。
_COUNTRY_LANG = {
    # 法语非洲
    "科特迪瓦": ("Ivory Coast", "fr"), "塞内加尔": ("Senegal", "fr"),
    "喀麦隆": ("Cameroon", "fr"), "马里": ("Mali", "fr"),
    "刚果（金）": ("DR Congo", "fr"), "刚果（布）": ("Congo", "fr"),
    "加蓬": ("Gabon", "fr"), "贝宁": ("Benin", "fr"), "多哥": ("Togo", "fr"),
    "布基纳法索": ("Burkina Faso", "fr"), "尼日尔": ("Niger", "fr"),
    "几内亚": ("Guinea", "fr"), "马达加斯加": ("Madagascar", "fr"),
    "乍得": ("Chad", "fr"),
    # 西语拉美
    "墨西哥": ("Mexico", "es"), "哥伦比亚": ("Colombia", "es"),
    "秘鲁": ("Peru", "es"), "智利": ("Chile", "es"), "阿根廷": ("Argentina", "es"),
    "厄瓜多尔": ("Ecuador", "es"), "玻利维亚": ("Bolivia", "es"),
    "委内瑞拉": ("Venezuela", "es"), "危地马拉": ("Guatemala", "es"),
    "多米尼加": ("Dominican Republic", "es"), "巴拉圭": ("Paraguay", "es"),
    "乌拉圭": ("Uruguay", "es"), "洪都拉斯": ("Honduras", "es"),
    "尼加拉瓜": ("Nicaragua", "es"), "巴拿马": ("Panama", "es"),
    "哥斯达黎加": ("Costa Rica", "es"), "萨尔瓦多": ("El Salvador", "es"),
    # 葡语
    "巴西": ("Brazil", "pt"), "安哥拉": ("Angola", "pt"), "莫桑比克": ("Mozambique", "pt"),
    # 阿语中东/北非
    "埃及": ("Egypt", "ar"), "沙特阿拉伯": ("Saudi Arabia", "ar"),
    "沙特": ("Saudi Arabia", "ar"), "阿联酋": ("United Arab Emirates", "ar"),
    "摩洛哥": ("Morocco", "ar"), "阿尔及利亚": ("Algeria", "ar"),
    "突尼斯": ("Tunisia", "ar"), "伊拉克": ("Iraq", "ar"), "约旦": ("Jordan", "ar"),
    "苏丹": ("Sudan", "ar"), "利比亚": ("Libya", "ar"), "也门": ("Yemen", "ar"),
    "阿曼": ("Oman", "ar"), "科威特": ("Kuwait", "ar"), "卡塔尔": ("Qatar", "ar"),
    "黎巴嫩": ("Lebanon", "ar"),
    # 英语/默认
    "尼日利亚": ("Nigeria", "en"), "肯尼亚": ("Kenya", "en"), "加纳": ("Ghana", "en"),
    "坦桑尼亚": ("Tanzania", "en"), "乌干达": ("Uganda", "en"),
    "埃塞俄比亚": ("Ethiopia", "en"), "赞比亚": ("Zambia", "en"),
    "津巴布韦": ("Zimbabwe", "en"), "南非": ("South Africa", "en"),
    "印度": ("India", "en"), "巴基斯坦": ("Pakistan", "en"),
    "孟加拉国": ("Bangladesh", "en"), "孟加拉": ("Bangladesh", "en"),
    "斯里兰卡": ("Sri Lanka", "en"), "尼泊尔": ("Nepal", "en"),
    "菲律宾": ("Philippines", "en"), "印度尼西亚": ("Indonesia", "en"),
    "印尼": ("Indonesia", "en"), "越南": ("Vietnam", "en"),
    "泰国": ("Thailand", "en"), "缅甸": ("Myanmar", "en"), "柬埔寨": ("Cambodia", "en"),
}
# 英文国家名 → 语言（target_countries 若直接存英文名时用）
_EN_LANG = {en: lang for (en, lang) in _COUNTRY_LANG.values()}

# 国家闸（#A）：渠道雷达找的是【目标市场里的海外买家/经销商】。
# 线索国家若是「已识别的真实国家、但不在该租户勾选的目标市场」→ 丢弃。
# 这样卖方本国(中国=供应商)、搜歪带进来的发达国家(Karcher德国)都被挡，
# 而且完全跟着客户在「目标市场」里选的大洲/国家走，不写死。
# 中→英国家映射：覆盖 tenant_ctx.REGIONS 全部 142 国 + 中国，值统一小写便于匹配。
_CN_EN = {
    # 非洲
    "尼日利亚": "nigeria", "埃塞俄比亚": "ethiopia", "埃及": "egypt",
    "刚果（金）": "dr congo", "坦桑尼亚": "tanzania", "肯尼亚": "kenya",
    "南非": "south africa", "乌干达": "uganda", "阿尔及利亚": "algeria",
    "苏丹": "sudan", "摩洛哥": "morocco", "安哥拉": "angola", "莫桑比克": "mozambique",
    "加纳": "ghana", "科特迪瓦": "ivory coast", "喀麦隆": "cameroon",
    "尼日尔": "niger", "布基纳法索": "burkina faso", "马里": "mali",
    "马拉维": "malawi", "赞比亚": "zambia", "塞内加尔": "senegal",
    "津巴布韦": "zimbabwe", "乍得": "chad", "几内亚": "guinea", "卢旺达": "rwanda",
    "贝宁": "benin", "布隆迪": "burundi", "突尼斯": "tunisia", "利比亚": "libya",
    "多哥": "togo", "塞拉利昂": "sierra leone", "厄立特里亚": "eritrea",
    "中非共和国": "central african republic", "毛里塔尼亚": "mauritania",
    "博茨瓦纳": "botswana", "纳米比亚": "namibia", "冈比亚": "gambia", "加蓬": "gabon",
    # 东南亚
    "印度尼西亚": "indonesia", "菲律宾": "philippines", "越南": "vietnam",
    "泰国": "thailand", "缅甸": "myanmar", "马来西亚": "malaysia",
    "柬埔寨": "cambodia", "老挝": "laos", "新加坡": "singapore",
    "文莱": "brunei", "东帝汶": "timor-leste",
    # 南亚
    "印度": "india", "巴基斯坦": "pakistan", "孟加拉国": "bangladesh",
    "尼泊尔": "nepal", "斯里兰卡": "sri lanka", "阿富汗": "afghanistan",
    "马尔代夫": "maldives",
    # 中东
    "土耳其": "turkey", "沙特阿拉伯": "saudi arabia", "阿联酋": "united arab emirates",
    "伊拉克": "iraq", "伊朗": "iran", "也门": "yemen", "叙利亚": "syria",
    "约旦": "jordan", "科威特": "kuwait", "阿曼": "oman", "卡塔尔": "qatar",
    "巴林": "bahrain", "黎巴嫩": "lebanon", "以色列": "israel", "巴勒斯坦": "palestine",
    # 北美
    "美国": "united states", "加拿大": "canada", "墨西哥": "mexico",
    # 拉丁美洲
    "巴西": "brazil", "哥伦比亚": "colombia", "阿根廷": "argentina", "秘鲁": "peru",
    "委内瑞拉": "venezuela", "智利": "chile", "厄瓜多尔": "ecuador",
    "玻利维亚": "bolivia", "巴拉圭": "paraguay", "乌拉圭": "uruguay",
    "危地马拉": "guatemala", "洪都拉斯": "honduras", "萨尔瓦多": "el salvador",
    "尼加拉瓜": "nicaragua", "哥斯达黎加": "costa rica", "巴拿马": "panama",
    "多米尼加共和国": "dominican republic", "古巴": "cuba", "海地": "haiti",
    "牙买加": "jamaica", "特立尼达和多巴哥": "trinidad and tobago",
    # 西欧
    "德国": "germany", "英国": "united kingdom", "法国": "france", "意大利": "italy",
    "西班牙": "spain", "荷兰": "netherlands", "比利时": "belgium", "瑞典": "sweden",
    "挪威": "norway", "丹麦": "denmark", "芬兰": "finland", "瑞士": "switzerland",
    "奥地利": "austria", "葡萄牙": "portugal", "希腊": "greece", "爱尔兰": "ireland",
    "捷克": "czech republic", "匈牙利": "hungary", "罗马尼亚": "romania",
    "波兰": "poland", "保加利亚": "bulgaria", "克罗地亚": "croatia",
    "斯洛伐克": "slovakia", "斯洛文尼亚": "slovenia", "塞尔维亚": "serbia",
    "波黑": "bosnia and herzegovina", "阿尔巴尼亚": "albania", "北马其顿": "north macedonia",
    # 东欧/中亚
    "俄罗斯": "russia", "乌克兰": "ukraine", "哈萨克斯坦": "kazakhstan",
    "白俄罗斯": "belarus", "乌兹别克斯坦": "uzbekistan", "阿塞拜疆": "azerbaijan",
    "格鲁吉亚": "georgia", "亚美尼亚": "armenia", "吉尔吉斯斯坦": "kyrgyzstan",
    "塔吉克斯坦": "tajikistan", "土库曼斯坦": "turkmenistan", "摩尔多瓦": "moldova",
    # 大洋洲
    "澳大利亚": "australia", "新西兰": "new zealand",
    "巴布亚新几内亚": "papua new guinea", "斐济": "fiji",
    # 卖方本国（永远不是买家）
    "中国": "china",
}
# 已识别的真实国家"宇宙"：在这里面、但不在租户目标市场 → 判为非目标、丢弃。
# 额外补几个 REGIONS 没有但线索常见的发达国家，确保它们能被识别并挡掉。
_KNOWN_EN = set(_CN_EN.values()) | {
    "china", "japan", "south korea", "taiwan", "hong kong", "macau",
}
# 没设目标市场时的兜底：至少挡掉卖方本国
_HOME_EN = {"china", "hong kong", "taiwan", "macau"}
# 线索里英文国家名的常见变体 → 规范名（小写）
_EN_ALIASES = {
    "usa": "united states", "u.s.a.": "united states", "u.s.": "united states",
    "us": "united states", "united states of america": "united states",
    "america": "united states",
    "uk": "united kingdom", "u.k.": "united kingdom", "great britain": "united kingdom",
    "britain": "united kingdom", "england": "united kingdom", "scotland": "united kingdom",
    "wales": "united kingdom",
    "uae": "united arab emirates", "u.a.e.": "united arab emirates",
    "drc": "dr congo", "democratic republic of the congo": "dr congo",
    "democratic republic of congo": "dr congo", "congo-kinshasa": "dr congo",
    "cote d'ivoire": "ivory coast", "côte d'ivoire": "ivory coast",
    "cote divoire": "ivory coast",
    "south korea": "south korea", "republic of korea": "south korea", "korea": "south korea",
    "russian federation": "russia", "viet nam": "vietnam", "burma": "myanmar",
    "prc": "china", "p.r. china": "china", "mainland china": "china",
    "hongkong": "hong kong",
}


def _norm_country(s):
    """线索国家名 → 规范小写名（吃掉常见变体/前缀）。"""
    c = (s or "").strip().lower()
    if not c:
        return ""
    if c.startswith("the "):
        c = c[4:].strip()
    return _EN_ALIASES.get(c, c)

# 太泛、当不了搜索钥匙的「品牌名」（城市/通用词），命中则降级用型号/产品词
_GENERIC_BRANDS = {
    "chongqing", "shanghai", "guangzhou", "beijing", "shandong", "zhejiang",
    "jiangsu", "china", "chinese", "asia", "sino", "global", "international",
    "power", "motor", "motors", "engine", "engines", "group", "machinery",
    "industry", "industrial", "trading", "auto", "vehicle", "new", "best",
    "top", "the", "and", "import", "export", "tech", "technology",
}

_A_HREF = re.compile(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                     re.IGNORECASE | re.DOTALL)
_TAGS   = re.compile(r"<[^>]+>")
_EMAIL  = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_PHONE_INTL = re.compile(r"\+?\d[\d\s().\-]{6,}\d")     # 粗判带区号的电话

_BRAND_SUFFIXES = (
    "engine", "engines", "motor", "motors", "machinery", "power", "group",
    "industry", "industrial", "intl", "international", "tech", "technology",
    "imp", "exp", "trade", "trading", "china", "global",
)


class CompetitorRadar:

    def __init__(self):
        self.deepseek_key     = ""
        self.serper_key       = ""
        self.hunter_key       = ""
        self.product_name     = ""
        self.search_keywords  = []
        self.target_countries = []
        self.my_category      = ""        # 本租户产品类目，给质量闸判断「同类」
        self.product_i18n     = {}        # 本租户产品多语言关键词，补搜本地市场竞品
        self._target_en       = set()     # 目标市场国家(英文小写)，国家闸用，run() 里按 target_countries 构建
        self.sources          = ["reverse", "social", "website", "customs"]
        self.proxy            = ""
        self.timeout          = 25
        self.max_dealer_pages = 3
        self.max_results      = 14
        self.max_workers      = 4
        # 质量闸阈值
        self.q_auto           = 60      # >= 自动入库（status=new）
        self.q_review         = 35      # >= 入库待确认（status=review），低于则丢弃
        self.website_baseline = 75      # 官网经销商页可信度高，给个基线分
        # 验真
        self.verify_sites     = True
        self.max_verify       = 15      # 每次最多验几个网址（控时间）
        # 额度统计
        self.serper_calls     = 0
        self.serper_fails     = 0
        self._ex              = AIExtractor()

    # ── 对外主入口 ───────────────────────────────────────────────────────────

    def run(self, urls=None, auto_search=False, want_intel=True,
            max_competitors=8) -> dict:
        self._ex.deepseek_key = self.deepseek_key
        self._ex.proxy        = self.proxy
        self.serper_calls     = 0
        self.serper_fails     = 0
        logger.info(f"[Radar] 开扫：sources={self.sources} serper_key={'有' if self.serper_key else '无'} "
                    f"deepseek_key={'有' if self.deepseek_key else '无'} "
                    f"目标国数={len(self.target_countries or [])} 类目={self.my_category or '(空)'}")
        # 国家闸：按租户在「目标市场」选的国家构建英文目标集
        self._target_en = {_CN_EN[c] for c in (self.target_countries or []) if c in _CN_EN}
        sources = [s for s in (self.sources or []) if s in _VIA] or list(_VIA)

        sites, seen = [], set()
        for u in (urls or []):
            d = self._norm_site(u)
            if d and d not in seen:
                seen.add(d); sites.append(d)
        if auto_search:
            for d in self.search_competitors():
                if d not in seen:
                    seen.add(d); sites.append(d)
        sites = sites[:max_competitors]

        def _process(site: str):
            ex = AIExtractor()
            ex.deepseek_key = self.deepseek_key
            ex.proxy        = self.proxy
            comp_host = urlparse(site).netloc.replace("www.", "")
            home  = ex.fetch_html(site)
            card  = self.build_intel(site, ex=ex, home=home) if want_intel else {}
            keys  = self._search_keys(card, site)
            brand = keys[0] if keys else self._pick_brand(card, site)

            leads = []
            if "website" in sources:
                leads += self.scrape_distributors(site, comp_host, ex=ex, home=home)
            if "reverse" in sources:
                leads += self.search_distributors(keys, comp_host, ex)
            if "social" in sources:
                leads += self.search_marketplaces(keys, comp_host, ex)
            if "customs" in sources:
                leads += self.customs_lookup(keys, comp_host, ex)
            leads = self._dedupe(leads)
            if card:
                card["distributor_count"] = len(leads)
                card["brand_used"] = brand
            return site, leads, card

        all_dist, all_intel, errors = [], [], []
        if not sites:
            return {"distributors": all_dist, "intel": all_intel,
                    "errors": errors, "sites": sites,
                    "serper_calls": self.serper_calls, "serper_fails": self.serper_fails}

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
                    logger.warning(f"[Radar] 处理 {site} 出错: {e}")

        # 跨竞品合并 + 大玩家加权置顶
        all_dist = self._rank_and_merge(all_dist)
        # 轻量验真滤死站
        if self.verify_sites:
            all_dist = self._verify_sites(all_dist)

        return {"distributors": all_dist, "intel": all_intel,
                "errors": errors, "sites": sites,
                "serper_calls": self.serper_calls, "serper_fails": self.serper_fails}

    # ── Serper 封装（带额度计数）─────────────────────────────────────────────

    def _serper(self, q: str, num: int = 10) -> list:
        if not self.serper_key:
            logger.warning("[Radar] 未配置 Serper Key，反向/社媒/海关源无法搜索（只能跑官网扫描）")
            return []
        self.serper_calls += 1
        try:
            resp = requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": self.serper_key,
                         "Content-Type": "application/json"},
                json={"q": q, "num": num}, timeout=20)
            if resp.status_code != 200:
                self.serper_fails += 1
                logger.warning(f"[Radar] Serper HTTP {resp.status_code} '{q[:40]}' "
                               f"响应:{resp.text[:120]}（key 无效/额度用尽时常见 401/403）")
                return []
            results = resp.json().get("organic", []) or []
            logger.info(f"[Radar] Serper '{q[:40]}' → {len(results)} 条结果")
            return results
        except Exception as e:
            self.serper_fails += 1
            logger.warning(f"[Radar] Serper 搜索异常 '{q[:40]}': {e}")
            return []

    # ── 自动搜竞品官网（auto_search）─────────────────────────────────────────

    def search_competitors(self, limit: int = 8) -> list:
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

        # 1) 收候选（连标题/摘要一起留着，给下一步类目验证用）
        cands = {}   # host -> {"url","title","snippet"}

        def _take(item):
            d = self._norm_site(item.get("link", ""))
            if not d:
                return
            host = urlparse(d).netloc.lower()
            if (self._is_platform(host) or is_invalid_competitor_url(d)
                    or "facebook" in host or "linkedin" in host):
                return
            if host not in cands:
                cands[host] = {"url": d, "title": item.get("title", ""),
                               "snippet": item.get("snippet", "")}

        for term in terms[:2]:
            for item in self._serper(
                    f"{term} manufacturer supplier OR factory OR distributor", num=10):
                _take(item)
            time.sleep(random.uniform(0.4, 0.9))
        # 多语言产品词补搜本地市场（法/西/葡/阿）的同行
        for lang, words in (self.product_i18n or {}).items():
            term = (words or [None])[0]
            if not term:
                continue
            for item in self._serper(
                    f"{term} fabricante OR distributeur OR موزع OR fabricant", num=8):
                _take(item)
            time.sleep(random.uniform(0.4, 0.9))

        if not cands:
            return []
        # 2) 类目验证：让 DeepSeek 只留"真做同类产品的同行"，过滤掉跨类目/目录/资讯站
        kept = self._verify_competitors(list(cands.values()))
        return kept[:limit]

    def _verify_competitors(self, cands: list) -> list:
        """用 DeepSeek 按产品类目筛候选竞品，只留真同行的域名 URL。"""
        if not (self.my_category and self.deepseek_key) or not cands:
            return [c["url"] for c in cands]      # 没类目/没 Key 就不筛
        lines, by_host = [], {}
        for c in cands:
            host = urlparse(c["url"]).netloc.replace("www.", "")
            by_host[host] = c["url"]
            lines.append(f"域名:{host} | 标题:{c['title']} | 摘要:{c['snippet']}")
        blob = "\n".join(lines)[:6000]
        instruction = (
            f"我的产品类目是「{self.my_category}」。下面每行是一个搜索到的网站"
            "（域名 | 标题 | 摘要）。请挑出真正是【生产/出口同类产品的厂商或同行】的，"
            "排除：不同品类的公司、B2B平台/目录站、资讯/百科/论坛、经销商个体店。"
            "只输出一个 JSON 数组，每个元素是 {\"domain\": \"合格的域名\"} 对象"
            "（域名照抄给出的）。没有合格的就返回 []。"
        )
        try:
            rows = self._ex.extract_from_text(blob, instruction, max_tokens=600)
        except Exception as e:
            logger.warning(f"[Radar] 竞品类目验证失败: {e}")
            return [c["url"] for c in cands]
        # rows 可能是 [{...}] 或 ["host",...]；两种都兼容
        ok = set()
        for r in rows:
            v = r if isinstance(r, str) else (r.get("domain") or r.get("host") or
                                              r.get("域名") or "")
            v = str(v).lower().replace("www.", "").strip()
            if v:
                ok.add(v)
        kept = [by_host[h] for h in by_host if h in ok]
        logger.info(f"[Radar] auto_search 候选 {len(cands)} 家 → 类目验证留下 {len(kept)} 家")
        return kept   # AI 判定都不合格就返回空，不硬塞杂质回来

    # ── 源①：反向经销商搜索（主引擎，本地化）────────────────────────────────

    def search_distributors(self, keys, comp_host: str, ex: AIExtractor) -> list:
        if not self.serper_key or not keys:
            return []
        primary = keys[0]
        results, seen = [], set()

        # 用品牌名/型号做通用经销商查询
        for k in keys[:2]:
            q = (f'"{k}" (distributor OR dealer OR "authorized dealer" '
                 f'OR reseller OR agent OR 经销商 OR 代理)')
            self._collect(q, results, seen, comp_host, skip_platform=True)

        # 按目标国家语言本地化查询
        for cn in (self.target_countries or [])[:3]:
            en, lang = self._country_info(cn)
            terms = _LANG_TERMS.get(lang, _LANG_TERMS["en"])
            q = f'"{primary}" ({" OR ".join(terms[:3])}) {en}'
            self._collect(q, results, seen, comp_host, skip_platform=True)

        return self._leads_from_results(primary, comp_host,
                                        results[:self.max_results], "reverse", ex)

    # ── 源②：本地电商 + 社媒 ─────────────────────────────────────────────────

    def search_marketplaces(self, keys, comp_host: str, ex: AIExtractor) -> list:
        if not self.serper_key or not keys:
            return []
        primary = keys[0]
        groups = [
            "site:facebook.com OR site:instagram.com OR site:linkedin.com",
            "site:jumia.com OR site:jumia.com.ng OR site:kilimall.com "
            "OR site:mercadolibre.com OR site:indiamart.com OR site:tradeindia.com",
        ]
        results, seen = [], set()
        for grp in groups:
            self._collect(f'"{primary}" ({grp})', results, seen, comp_host,
                          skip_platform=False)
        # 带联系方式倾向（WhatsApp/电话）
        self._collect(
            f'"{primary}" (dealer OR reseller OR 经销) (whatsapp OR contact OR phone)',
            results, seen, comp_host, skip_platform=True)
        return self._leads_from_results(primary, comp_host,
                                        results[:self.max_results], "social", ex)

    # ── 源④：海关反查（公开聚合页）──────────────────────────────────────────

    def customs_lookup(self, keys, comp_host: str, ex: AIExtractor) -> list:
        if not self.serper_key or not keys:
            return []
        primary = keys[0]
        results, seen = [], set()
        self._collect(
            f'"{primary}" (site:importyeti.com OR site:zauba.com OR site:panjiva.com '
            f'OR site:seair.co.in OR site:volza.com)',
            results, seen, comp_host, skip_platform=False)
        self._collect(
            f'"{primary}" (buyer OR consignee OR importer) '
            f'(customs OR shipment OR "bill of lading")',
            results, seen, comp_host, skip_platform=False)
        return self._leads_from_results(primary, comp_host,
                                        results[:12], "customs", ex)

    def _collect(self, q, results, seen, comp_host, skip_platform=True):
        """跑一次 Serper，把合格结果累加进 results（去重、可选跳平台/竞品自己）。"""
        for it in self._serper(q, num=10):
            link = it.get("link", "")
            host = urlparse(link).netloc.lower()
            if not link or link in seen:
                continue
            if comp_host and comp_host in host:
                continue
            if skip_platform and self._is_platform(host):
                continue
            seen.add(link)
            results.append({"title": it.get("title", ""),
                            "snippet": it.get("snippet", ""), "link": link})
        time.sleep(random.uniform(0.3, 0.7))

    # ── 搜索结果 → DeepSeek 批量提取（带匹配度打分）──────────────────────────

    def _leads_from_results(self, brand, comp_host, results, via, ex):
        if not results:
            return []
        label = _VIA.get(via, via)
        cat = self.my_category or "同类产品"
        blob = "\n".join(
            f"标题:{r.get('title','')} | 摘要:{r.get('snippet','')} | 链接:{r.get('link','')}"
            for r in results)[:7000]
        instruction = (
            f"下面是搜索引擎关于品牌「{brand}」海外渠道的结果，每行一个结果"
            "（标题 | 摘要 | 链接）。请提取【正在销售/代理/进口/零售】相关产品的"
            "海外公司或商家，作为潜在客户线索。每个对象字段："
            "company_name(公司或商家名,别用文章标题/平台名/人名), country(国家,英文), "
            "city(城市), phone(电话,优先带国际区号或 WhatsApp 号), email(邮箱), "
            "website(优先用该结果的链接), relevance(0-100整数), reason(一句话中文理由).\n"
            f"relevance 打分硬规则（我的产品类目是「{cat}」）：\n"
            f"- 它自己卖的就是「{cat}」或其配件，且是经销商/批发商/进口商/零售商 → 70-95；\n"
            "- 它卖的是【别的品类】(如农机/割草机/发电机/水泵/清洁设备/汽车整车/无关机械) "
            "→ relevance ≤ 25，哪怕它出现在结果里也要给低分；\n"
            "- 它本身是【制造厂/工厂/生产商】(跟我同类的生产者=同行或供应商，不是买家) → ≤ 30；\n"
            "- 公司名【像人名、单个泛词、媒体频道(TV/Channel)、含糊无法确认是商家】 → ≤ 30；\n"
            "- 只是新闻/百科/招聘/论坛/平台分类目录页 → ≤ 20。\n"
            f"排除品牌方「{brand}」自己。只依据给出内容,宁缺毋滥、绝不编造。没有就返回[]。"
        )
        try:
            data = ex.extract_from_text(blob, instruction, max_tokens=1800)
        except Exception as e:
            logger.warning(f"[Radar] {comp_host} · {label} AI 提取失败: {e}")
            return []

        rows, seen, dropped = [], set(), 0
        for d in data:
            try:
                score = int(float(d.get("relevance", 50)))
            except Exception:
                score = 50
            lead = self._to_lead(d, brand, comp_host, via,
                                 score=score, reason=str(d.get("reason", "")))
            if not lead:
                dropped += 1
                continue
            key = (lead["company_name"].lower(), (lead.get("country") or "").lower())
            if key in seen:
                continue
            seen.add(key); rows.append(lead)
        logger.info(f"[Radar] {comp_host} · {label} → {len(rows)} 家（低分丢弃 {dropped}）")
        return rows

    # ── 源③：官网扫描（逐页 AI 提取）────────────────────────────────────────

    def scrape_distributors(self, site, comp_host, ex=None, home=None):
        ex = ex or self._ex
        home = home if home is not None else ex.fetch_html(site)
        if not home:
            logger.warning(f"[Radar] 抓不到首页: {site}")
            return []
        pages = self._find_dealer_pages(site, home) or [site]
        instruction = (
            "提取页面里所有经销商/代理商/分销商/门店/合作伙伴的信息。字段："
            "company_name(公司或门店名), country(国家,英文), city(城市), "
            "phone(电话,优先带区号), email(邮箱), website(网站). "
            "只提取页面里真实出现的信息，没有的字段留空，不要编造。"
            "如果页面没有经销商名单就返回 []。"
        )
        rows, seen = [], set()
        for purl in pages[:self.max_dealer_pages]:
            try:
                data = ex.extract(purl, instruction, max_tokens=1500)
            except Exception as e:
                logger.warning(f"[Radar] 提取 {purl} 失败: {e}")
                continue
            for d in data:
                # 官网自己挂出的经销商页可信度高，给基线分
                lead = self._to_lead(d, "", comp_host, "website",
                                     score=self.website_baseline, reason="竞品官网公开经销商")
                if not lead:
                    continue
                key = (lead["company_name"].lower(), (lead.get("country") or "").lower())
                if key in seen:
                    continue
                seen.add(key); rows.append(lead)
            time.sleep(random.uniform(0.2, 0.5))
        logger.info(f"[Radar] {comp_host} · 官网经销商 → {len(rows)} 家")
        return rows

    def _find_dealer_pages(self, base, home_html):
        cands, seen = [], set()
        for href, text in _A_HREF.findall(home_html):
            label = (_TAGS.sub(" ", text) + " " + href).lower()
            if any(h in label for h in _DEALER_HINTS):
                full = urljoin(base, href.strip())
                if not full.startswith(("http://", "https://")):
                    continue
                if urlparse(full).netloc.replace("www.", "") != \
                   urlparse(base).netloc.replace("www.", ""):
                    continue
                key = full.split("#")[0]
                if key not in seen:
                    seen.add(key); cands.append(key)
        return cands[:self.max_dealer_pages]

    # ── 竞品情报卡（含品牌名 + 主力型号）────────────────────────────────────

    def build_intel(self, site, dist_count=0, ex=None, home=None):
        ex = ex or self._ex
        host = urlparse(site).netloc.replace("www.", "")
        home = home if home is not None else ex.fetch_html(site)
        if not home:
            return {}
        text = ex.html_to_text(home, max_chars=5000)

        about = ""
        for href, label in _A_HREF.findall(home):
            l = (_TAGS.sub(" ", label) + " " + href).lower()
            if any(h in l for h in _ABOUT_HINTS):
                full = urljoin(site, href.strip())
                if full.startswith("http"):
                    about = ex.html_to_text(ex.fetch_html(full), max_chars=3000)
                    break

        blob = (text + "\n\n" + about).strip()[:7000]
        instruction = (
            "下面是一家公司官网的正文。请总结这家公司，只输出一个对象的 JSON 数组"
            "（数组里就一个对象），字段："
            "brand(品牌名或公司简称,英文,尽量短,用于全网搜它的经销商), "
            "company_name(公司全称), "
            "models(主力产品型号清单,字符串数组,如 [\"CG125\",\"CB250\",\"152F\"],"
            "没有就空数组), "
            "main_products(主营产品,中文一句话), "
            "target_markets(主攻市场/国家,中文), "
            "company_scale(规模线索,如成立年份/员工/产能,没有就留空), "
            "price_signal(价格或定位线索,没有就留空), "
            "highlights(亮点或最新动态,中文一句话). 只依据正文,不要编造。"
        )
        cards = ex.extract_from_text(blob, instruction, max_tokens=800)
        card = cards[0] if cards else {}
        card.setdefault("brand", "")
        card.setdefault("company_name", "")
        card.setdefault("models", [])
        card.setdefault("main_products", "")
        card["competitor"]        = host
        card["url"]               = site
        card["distributor_count"] = dist_count
        return card

    # ── 搜索钥匙：品牌名 + 型号（品牌太泛时降级）────────────────────────────

    def _search_keys(self, card, site):
        keys = []
        brand = self._pick_brand(card, site)
        if brand and not self._is_generic_brand(brand):
            keys.append(brand)
        models = card.get("models") or []
        if isinstance(models, str):
            models = [m.strip() for m in re.split(r"[,，;；/]+", models) if m.strip()]
        for m in models[:3]:
            m = str(m).strip()
            if m and 2 <= len(m) <= 20 and m not in keys:
                keys.append(m)
        if not keys:                       # 没有像样的钥匙：退到产品词，再退品牌
            if self.product_name:
                keys.append(self.product_name)
            elif brand:
                keys.append(brand)
        return keys[:4]

    @staticmethod
    def _is_generic_brand(name):
        n = (name or "").strip().lower()
        if len(n) < 3:
            return True
        toks = [t for t in re.split(r"[\s\-_]+", n) if t]
        return all(t in _GENERIC_BRANDS for t in toks) if toks else True

    def _country_info(self, name):
        """目标国家名（中/英）→ (英文名, 语言)。查不到则原样 + 英语。"""
        name = (name or "").strip()
        if name in _COUNTRY_LANG:
            return _COUNTRY_LANG[name]
        if name in _EN_LANG:
            return (name, _EN_LANG[name])
        return (name, "en")

    # ── 跨竞品合并 + 大玩家加权置顶（#6）────────────────────────────────────

    def _rank_and_merge(self, leads):
        if not leads:
            return leads
        bucket, comps = {}, {}
        for l in leads:
            key  = (l["company_name"].lower(), (l.get("country") or "").lower())
            comp = l.get("_comp", "")
            if key not in bucket:
                bucket[key] = l; comps[key] = set()
            else:
                base = bucket[key]
                for f in ("country", "city", "website", "email", "phone"):
                    if not base.get(f) and l.get(f):
                        base[f] = l[f]
                base["_score"] = max(base.get("_score", 0), l.get("_score", 0))
            if comp:
                comps[key].add(comp)
        out = []
        for key, l in bucket.items():
            n = len(comps[key])
            if n > 1:
                l["notes"] = (l.get("notes") or "") + f" | 🔥代理{n}个竞品"
                l["_score"] = min(100, l.get("_score", 60) + 10 * (n - 1))
            out.append(l)
        out.sort(key=lambda x: x.get("_score", 0), reverse=True)
        return out

    # ── 轻量验真：滤掉打不开的死站（#7）──────────────────────────────────────

    def _verify_sites(self, leads):
        if not leads:
            return leads
        checked, out = 0, []
        for l in leads:
            site = l.get("website")
            # 高可信(>=90,如官网经销商/大玩家)跳过验真省时间；只验中低分带网址的
            if site and checked < self.max_verify and l.get("_score", 100) < 90:
                checked += 1
                if not self._alive(site):
                    l["_score"] = l.get("_score", 60) - 25
                    l["notes"] = (l.get("notes") or "") + " | ⚠️网站未响应"
            if l.get("_score", 60) >= self.q_review:
                out.append(l)
        return out

    @staticmethod
    def _alive(url):
        try:
            r = requests.head(url, timeout=6, allow_redirects=True,
                              headers={"User-Agent": _UA})
            if r.status_code < 400:
                return True
            r = requests.get(url, timeout=8, allow_redirects=True, stream=True,
                             headers={"User-Agent": _UA})
            return r.status_code < 400
        except Exception:
            return False

    # ── 原始记录 → 标准 lead dict（含质量闸 + 状态分流）──────────────────────

    def _to_lead(self, d, brand, comp_host, via, score=None, reason=""):
        name = (d.get("company_name") or d.get("name") or "").strip()
        if not name or len(name) < 2:
            return None
        low = name.lower()
        if low in ("n/a", "none", "null", "-", "company_name", "distributor",
                   "dealer", "reseller", "importer"):
            return None
        if brand and low == brand.strip().lower():
            return None

        country = (d.get("country") or "").strip()[:60]
        city    = (d.get("city") or "").strip()[:60]
        # 国家闸（#A）：按租户目标市场过滤——已识别的真实国家但不在目标市场 → 丢。
        # 没设目标市场时只兜底挡卖方本国(中国)。空国家给好处，不罚。
        cl = _norm_country(country)
        if cl:
            if self._target_en:
                if cl not in self._target_en and cl in _KNOWN_EN:
                    return None
            elif cl in _HOME_EN:
                return None

        if score is None:
            score = 50
        score = max(0, min(100, int(score)))
        # 质量闸：低于待确认线直接丢弃
        if score < self.q_review:
            return None
        status = "new" if score >= self.q_auto else "review"

        email = (d.get("email") or "").strip()
        if email and not _EMAIL.match(email):
            email = ""
        phone = (d.get("phone") or d.get("tel") or "").strip()[:60]
        website = (d.get("website") or "").strip()
        if website and not website.startswith(("http://", "https://")):
            website = "http://" + website

        label = _VIA.get(via, via)
        notes = f"渠道雷达·{label} | 竞品 {comp_host}"
        if brand and via != "website":
            notes += f" | 在卖「{brand}」"
        if city:
            notes += f" | {city}"
        notes += f" | 匹配度{score}"
        if status == "review":
            notes += "·待确认"
        if reason:
            notes += f" | {reason.strip()[:40]}"
        if phone and _PHONE_INTL.search(phone):
            notes += " | 📱可WhatsApp触达"

        return {
            "company_name": name[:200],
            "country":      country or None,
            "city":         city or None,
            "website":      website or None,
            "email":        email or None,
            "phone":        phone or None,
            "sources":      ["competitor_radar"],
            "status":       status,
            "notes":        notes,
            "_score":       score,        # 内部排序用，cleaner 会忽略
            "_comp":        comp_host,     # 内部跨竞品统计用
        }

    @staticmethod
    def _dedupe(leads):
        out, seen = [], set()
        for l in leads:
            key = (l["company_name"].lower(), (l.get("country") or "").lower())
            if key in seen:
                continue
            seen.add(key); out.append(l)
        return out

    @staticmethod
    def _is_platform(host):
        return any(p in host for p in _PLATFORM_HOSTS)

    def _pick_brand(self, card, site):
        for k in ("brand", "company_name"):
            b = (card.get(k) or "").strip()
            if (b and 2 <= len(b) <= 40
                    and not b.lower().startswith(("http", "www"))
                    and len(b.split()) <= 4):
                return b
        return self._brand_from_host(urlparse(site).netloc)

    @staticmethod
    def _brand_from_host(host):
        name = (host or "").replace("www.", "").split(".")[0].lower()
        for suf in sorted(_BRAND_SUFFIXES, key=len, reverse=True):
            if name.endswith(suf) and len(name) > len(suf) + 2:
                name = name[:-len(suf)]
                break
        name = name.replace("-", " ").replace("_", " ").strip()
        return name.title() if name else (host or "").split(".")[0]

    @staticmethod
    def _norm_site(u):
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
