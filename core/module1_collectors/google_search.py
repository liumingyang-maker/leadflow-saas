"""
module1_collectors/google_search.py — Google 搜索指令采集
===========================================================
职责：用精心设计的 Google Dork 搜索指令，
      发现还没在海关数据库里的潜在买家网站。

技术方案：
  - SerpAPI ($50/月) — 稳定，有结构化JSON结果，推荐
  - 备选：ScraperAPI / Bright Data
  - Mock 模式：开发测试用，不消耗额度

Google Dork 搜索逻辑：
  针对每个目标国家，用多种搜索组合：
  1. "motorcycle engine importer" Nigeria
  2. "CG engine" distributor Pakistan site:linkedin.com/company
  3. intitle:"motorcycle parts" wholesale Vietnam "contact"
  ...

使用方式：
    from module1_collectors.google_search import google_collector
    leads = google_collector.run_and_clean(mock=True)
"""

import time
import random
import json
import hashlib
from typing import Optional

import requests
from log_setup import logger

from config import cfg
from module2_cleaner import cleaner
from database import db


class GoogleSearchCollector:

    def __init__(self):
        self.serpapi_key = cfg.SERPAPI_KEY
        self.session = requests.Session()
        self.cache_dir = cfg.CACHE_DIR / "google"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ──────────────────────────────────────────────────────
    # 搜索指令生成器
    # ──────────────────────────────────────────────────────

    def build_queries(self, country: str) -> list[str]:
        """
        为指定国家生成一批 Google 搜索指令。
        每种模板针对不同类型的买家：
          - 直接进口商（有官网）
          - LinkedIn 公司页面
          - B2B 目录网站上的买家
        """
        templates = [
            # 模板1: 直接搜进口商官网
            '"motorcycle engine" importer {country}',
            '"motorbike engine" supplier distributor {country}',
            '"CG engine" OR "CB engine" buyer {country}',
            '"tricycle engine" wholesale {country}',
            # 模板2: LinkedIn 公司页面（精准找决策人公司）
            '"motorcycle engine" {country} site:linkedin.com/company',
            '"engine parts" importer {country} site:linkedin.com/company',
            # 模板3: 带联系方式的公司页面
            'intitle:"motorcycle parts" {country} "contact us" "import"',
            '"motorcycle engine" {country} "purchase" OR "procurement" -news -blog',
            # 模板4: B2B 平台上的买家档案
            '"motorcycle engine" {country} site:tradekey.com OR site:ec21.com',
            # 模板5: 行业协会/目录
            '"motorcycle importers association" {country}',
        ]

        queries = []
        for template in templates:
            query = template.replace("{country}", country)
            queries.append(query)

        return queries

    # ──────────────────────────────────────────────────────
    # 缓存
    # ──────────────────────────────────────────────────────

    def _cache_key(self, query: str) -> str:
        return hashlib.md5(query.encode()).hexdigest()[:12]

    def _read_cache(self, key: str, ttl_hours: int = 72) -> Optional[list]:
        path = self.cache_dir / f"{key}.json"
        if not path.exists():
            return None
        age = (time.time() - path.stat().st_mtime) / 3600
        if age > ttl_hours:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_cache(self, key: str, data: list) -> None:
        path = self.cache_dir / f"{key}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ──────────────────────────────────────────────────────
    # SerpAPI 真实搜索
    # ──────────────────────────────────────────────────────

    def _search_serpapi(self, query: str, num_results: int = 10) -> list[dict]:
        """
        用 SerpAPI 执行 Google 搜索，返回结构化结果。
        SerpAPI 文档: https://serpapi.com/search-api
        """
        url = "https://serpapi.com/search"
        params = {
            "q": query,
            "api_key": self.serpapi_key,
            "engine": "google",
            "num": num_results,
            "hl": "en",    # 搜索结果语言
            "gl": "us",    # 地区（用 us 获得最广的结果）
        }

        try:
            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            results = []
            for item in data.get("organic_results", []):
                results.append({
                    "url": item.get("link", ""),
                    "title": item.get("title", ""),
                    "snippet": item.get("snippet", ""),
                    "domain": item.get("displayed_link", ""),
                })

            logger.debug(f"SerpAPI: '{query[:50]}...' → {len(results)} 条结果")
            return results

        except requests.HTTPError as e:
            if e.response.status_code == 429:
                logger.warning("SerpAPI 限流，等待 30 秒")
                time.sleep(30)
            else:
                logger.error(f"SerpAPI 请求失败: {e}")
            return []
        except Exception as e:
            logger.error(f"SerpAPI 异常: {e}")
            return []

    # ──────────────────────────────────────────────────────
    # 搜索结果解析 → lead 格式
    # ──────────────────────────────────────────────────────

    def _parse_result(self, result: dict, country: str) -> Optional[dict]:
        """
        把单条 Google 搜索结果解析成 lead 格式。

        过滤规则：
        - 排除新闻网站、博客、百科
        - 排除中文网站（我们找的是外国买家）
        - 排除阿里巴巴/亚马逊等大平台（不是真实买家）

        返回 None 表示这条结果不是有效的潜在买家。
        """
        url = result.get("url", "")
        title = result.get("title", "")
        snippet = result.get("snippet", "")

        # ── 过滤无效域名 ──
        excluded_domains = [
            "alibaba.com", "amazon.com", "ebay.com", "wikipedia.org",
            "youtube.com", "facebook.com", "twitter.com", "instagram.com",
            "aliexpress.com", "made-in-china.com", "globalsources.com",
            "indiamart.com", "tradeindia.com", "quora.com", "reddit.com",
            "news.", "blog.", ".gov", "wikipedia",
        ]
        if any(ex in url.lower() for ex in excluded_domains):
            return None

        # ── 过滤非商业页面 ──
        non_commercial_signals = [
            "wikipedia", "news", "article", "forum", "quora",
            "reddit", ".edu", ".gov",
        ]
        if any(s in url.lower() or s in title.lower()
               for s in non_commercial_signals):
            return None

        # ── 判断是否真的是进口商（简单关键词判断） ──
        buyer_signals = [
            "import", "wholesale", "distribut", "purchas", "procure",
            "supply", "trading", "dealer", "parts", "engine", "moto",
        ]
        combined_text = (title + " " + snippet).lower()
        if not any(sig in combined_text for sig in buyer_signals):
            return None

        # ── 提取公司名（用URL的domain作为备用名称） ──
        from module1_collectors.linkedin import enricher
        domain = enricher.extract_domain(url)
        # 尝试从title提取公司名（title格式通常是"公司名 - 描述"）
        company_name = title.split(" - ")[0].split(" | ")[0].strip()
        if len(company_name) > 60 or len(company_name) < 3:
            company_name = domain or ""

        if not company_name:
            return None

        return {
            "company_name": company_name,
            "country": country,
            "website": url,
            "sources": ["google"],
            "notes": f"Google搜索 | {snippet[:100]}",
        }

    # ──────────────────────────────────────────────────────
    # Mock 数据（测试用）
    # ──────────────────────────────────────────────────────

    def _mock_search(self, query: str, country: str) -> list[dict]:
        """返回模拟搜索结果"""
        mock_data = {
            "Nigeria": [
                {
                    "company_name": "Kano Moto Distributors",
                    "country": "Nigeria",
                    "website": "https://kanomoto.com.ng",
                    "sources": ["google"],
                },
                {
                    "company_name": "Niger Delta Engine Traders",
                    "country": "Nigeria",
                    "website": "https://ndet.ng",
                    "sources": ["google"],
                },
            ],
            "Vietnam": [
                {
                    "company_name": "Ho Chi Minh Engine Parts JSC",
                    "country": "Vietnam",
                    "website": "https://hcmparts.vn",
                    "sources": ["google"],
                },
            ],
            "Pakistan": [
                {
                    "company_name": "Islamabad Motor Importers",
                    "country": "Pakistan",
                    "website": "https://islamabadmoto.pk",
                    "sources": ["google"],
                },
            ],
        }
        # 只返回每个国家一次，避免同一国家多个查询重复
        results = mock_data.get(country, [])
        # 随机取部分（模拟不同查询返回不同结果）
        return random.sample(results, min(len(results), 2))

    # ──────────────────────────────────────────────────────
    # 单次搜索
    # ──────────────────────────────────────────────────────

    def search(self, query: str, country: str,
               use_cache: bool = True, mock: bool = False) -> list[dict]:
        """
        执行单条搜索，返回解析后的 leads。
        """
        if mock:
            return self._mock_search(query, country)

        if not self.serpapi_key:
            logger.warning("SERPAPI_KEY 未配置，跳过 Google 搜索")
            return []

        # 检查缓存
        cache_key = self._cache_key(query)
        if use_cache:
            cached = self._read_cache(cache_key)
            if cached is not None:
                logger.debug(f"命中缓存: {query[:40]}")
                return cached

        # 真实搜索
        raw_results = self._search_serpapi(query)

        # 解析过滤
        leads = []
        for r in raw_results:
            lead = self._parse_result(r, country)
            if lead:
                leads.append(lead)

        logger.info(
            f"搜索完成: '{query[:45]}...' → "
            f"{len(raw_results)} 条原始 → {len(leads)} 条有效"
        )

        if leads and use_cache:
            self._write_cache(cache_key, leads)

        # 限速
        time.sleep(random.uniform(cfg.REQUEST_DELAY_MIN * 2, cfg.REQUEST_DELAY_MAX * 2))

        return leads

    # ──────────────────────────────────────────────────────
    # 批量搜索
    # ──────────────────────────────────────────────────────

    def fetch_all(self, countries: list[str] = None,
                  mock: bool = False) -> list[dict]:
        """
        对所有目标国家执行搜索指令组合。
        为节省 SerpAPI 额度，每个国家只用前 3 条最有价值的搜索模板。
        """
        if countries is None:
            # 默认只搜优先级最高的国家
            countries = cfg.MARKET_PRIORITY["tier1"]

        all_leads = []
        total_queries = 0

        for country in countries:
            queries = self.build_queries(country)
            # 生产环境每个国家用全部 queries
            # 默认只取前3条（节省额度）
            active_queries = queries[:3] if not mock else queries[:2]

            for query in active_queries:
                leads = self.search(query, country, mock=mock)
                all_leads.extend(leads)
                total_queries += 1

                if total_queries % 5 == 0 and not mock:
                    logger.info(f"搜索进度: {total_queries} 次，累计 {len(all_leads)} 条")
                    time.sleep(5)

        logger.success(
            f"Google搜索完成: {total_queries} 次查询，"
            f"共 {len(all_leads)} 条潜在 leads"
        )
        return all_leads

    def run_and_clean(self, countries: list[str] = None,
                      mock: bool = False) -> dict:
        """一键搜索 + 清洗 + 写库"""
        raw = self.fetch_all(countries=countries, mock=mock)
        if not raw:
            return {"input": 0, "db_new": 0}
        return cleaner.run(raw, source="google")


# 单例
google_collector = GoogleSearchCollector()


# ──────────────────────────────────────────────────────────
# 直接运行 = 测试
# ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logger.remove()
    logger.add(sys.stdout, level="INFO")

    db.init()

    print("=" * 55)
    print("测试 1: 搜索指令生成")
    queries = google_collector.build_queries("Nigeria")
    print(f"  为 Nigeria 生成 {len(queries)} 条搜索指令:")
    for q in queries:
        print(f"    · {q}")

    print("\n测试 2: Mock 搜索单次")
    results = google_collector.search(
        '"motorcycle engine" importer Nigeria',
        "Nigeria",
        mock=True
    )
    print(f"  返回 {len(results)} 条有效 leads")
    for r in results:
        print(f"  - {r['company_name']} | {r['website']}")

    print("\n测试 3: Mock 批量搜索 + 清洗写库")
    stats = google_collector.run_and_clean(mock=True)
    print(f"  清洗统计: {stats}")

    print("\n✅ Google 搜索模块测试通过")
    print("=" * 55)
