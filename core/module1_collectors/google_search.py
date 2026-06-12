"""
module1_collectors/google_search.py
────────────────────────────────────
搜索引擎采集器（Serper.dev + DeepSeek 解析）

流程：
  1. 根据租户的产品/关键词，为每个目标国家生成搜索词
  2. 调用 Serper.dev（谷歌搜索API，2500次免费）获取原始结果
  3. 把原始结果丢给 DeepSeek，让它判断哪些是真正的进口商并提取信息
  4. 返回结构化的 leads 列表
"""

import time
import json
import hashlib
import random
import requests
from pathlib import Path
from typing import Optional


class SerperDeepSeekCollector:

    def __init__(self):
        self.serper_key   = ""
        self.deepseek_key = ""
        self.product_name  = ""      # 租户产品名，用于生成搜索词
        self.search_keywords = []    # 租户自定义关键词
        self._cache: dict = {}

    # ────────────────────────────────────────────────────
    # 搜索词生成
    # ────────────────────────────────────────────────────

    def _build_queries(self, country: str) -> list:
        """根据租户产品和目标国家生成搜索词"""
        product = self.product_name or "machinery"
        base_kws = self.search_keywords[:3] if self.search_keywords else [product]

        queries = []
        for kw in base_kws:
            queries += [
                f'"{kw}" importer {country}',
                f'"{kw}" distributor {country} wholesale',
                f'"{kw}" buyer {country} "contact us"',
                f'"{kw}" {country} site:linkedin.com/company',
            ]
        return queries[:6]  # 每国最多6条，节省额度

    # ────────────────────────────────────────────────────
    # Serper.dev 搜索
    # ────────────────────────────────────────────────────

    def _serper_search(self, query: str) -> list:
        """调用 Serper.dev，返回原始搜索结果列表"""
        cache_key = hashlib.md5(query.encode()).hexdigest()[:10]
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            resp = requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": self.serper_key, "Content-Type": "application/json"},
                json={"q": query, "num": 10, "hl": "en", "gl": "us"},
                timeout=20,
            )
            resp.raise_for_status()
            items = resp.json().get("organic", [])
            results = [
                {"title": r.get("title",""), "url": r.get("link",""), "snippet": r.get("snippet","")}
                for r in items
            ]
            self._cache[cache_key] = results
            time.sleep(random.uniform(0.5, 1.2))
            return results
        except Exception as e:
            print(f"[Serper] 搜索失败: {e}")
            return []

    # ────────────────────────────────────────────────────
    # DeepSeek 解析
    # ────────────────────────────────────────────────────

    def _deepseek_parse(self, results: list, country: str) -> list:
        """
        把搜索结果列表发给 DeepSeek，
        让它判断哪些是真正的进口商/买家并提取结构化信息。
        """
        if not results or not self.deepseek_key:
            return self._fallback_parse(results, country)

        # 把搜索结果压缩成文本
        results_text = "\n".join([
            f"{i+1}. 标题：{r['title']}\n   网址：{r['url']}\n   摘要：{r['snippet']}"
            for i, r in enumerate(results[:8])
        ])

        prompt = f"""你是一个外贸采购商识别专家。
以下是针对"{country}"的谷歌搜索结果，我需要找的是该国真实的进口商/采购商/分销商公司。

搜索结果：
{results_text}

请从中筛选出看起来是真实企业（进口商/分销商/批发商）的条目，排除以下内容：
- 新闻、博客、论坛、维基百科
- 阿里巴巴、亚马逊等大型平台
- 政府网站、协会（除非是买家协会目录）

对每个有效企业，用JSON格式输出，每行一个：
{{"company_name": "公司名", "website": "网址", "country": "{country}", "notes": "一句话描述"}}

只输出JSON行，不要其他文字。如果没有有效企业，输出空。"""

        try:
            resp = requests.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.deepseek_key}",
                         "Content-Type": "application/json"},
                json={
                    # deepseek-chat 将于 2026-07-24 弃用 → 迁到 deepseek-v4-flash。
                    # 关思考模式：批量解析要快+省（flash 默认开思考会多花 reasoning token）。
                    "model": "deepseek-v4-flash",
                    "thinking": {"type": "disabled"},
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_tokens": 800,
                },
                timeout=30,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()

            leads = []
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("{"):
                    try:
                        lead = json.loads(line)
                        if lead.get("company_name") and lead.get("website"):
                            lead["source"] = "google"
                            leads.append(lead)
                    except Exception:
                        pass
            return leads

        except Exception as e:
            print(f"[DeepSeek] 解析失败: {e}，使用规则解析")
            return self._fallback_parse(results, country)

    def _fallback_parse(self, results: list, country: str) -> list:
        """DeepSeek 不可用时的规则解析备用方案"""
        excluded = {"alibaba","amazon","ebay","wikipedia","youtube","facebook",
                    "aliexpress","made-in-china","globalsources","indiamart",
                    "quora","reddit","twitter","instagram"}
        buyer_signals = ["import","wholesale","distribut","purchas","supply",
                         "trading","dealer","parts","engine","moto","machinery"]
        leads = []
        for r in results:
            url = r.get("url","").lower()
            if any(ex in url for ex in excluded):
                continue
            text = (r.get("title","") + " " + r.get("snippet","")).lower()
            if not any(sig in text for sig in buyer_signals):
                continue
            company_name = r["title"].split(" - ")[0].split(" | ")[0].strip()
            if 3 <= len(company_name) <= 60:
                leads.append({
                    "company_name": company_name,
                    "website": r["url"],
                    "country": country,
                    "source": "google",
                    "notes": r.get("snippet","")[:100],
                })
        return leads

    # ────────────────────────────────────────────────────
    # Mock 数据
    # ────────────────────────────────────────────────────

    def _mock_results(self, country: str) -> list:
        mock = {
            "尼日利亚": [
                {"company_name":"Kano Moto Distributors","website":"https://kanomoto.com.ng","country":"尼日利亚","source":"google","notes":"摩托车配件进口商"},
                {"company_name":"Niger Delta Engine Traders","website":"https://ndet.ng","country":"尼日利亚","source":"google","notes":"发动机批发"},
            ],
            "越南": [
                {"company_name":"Ho Chi Minh Engine Parts JSC","website":"https://hcmparts.vn","country":"越南","source":"google","notes":"摩托车零件进口"},
            ],
            "巴基斯坦": [
                {"company_name":"Islamabad Motor Importers","website":"https://islamabadmoto.pk","country":"巴基斯坦","source":"google","notes":"摩托车进口商"},
            ],
        }
        return mock.get(country, [])

    # ────────────────────────────────────────────────────
    # 主入口
    # ────────────────────────────────────────────────────

    def fetch_all(self, countries: list = None, mock: bool = False) -> list:
        """对目标国家批量采集，返回 leads 列表"""
        if not countries:
            return []

        if mock or not self.serper_key:
            results = []
            for c in countries[:5]:
                results.extend(self._mock_results(c))
            return results

        all_leads = []
        for country in countries:
            queries = self._build_queries(country)
            country_raw = []

            for query in queries:
                raw = self._serper_search(query)
                country_raw.extend(raw)

            # 去重（同一URL只处理一次）
            seen_urls = set()
            unique_raw = []
            for r in country_raw:
                if r["url"] not in seen_urls:
                    seen_urls.add(r["url"])
                    unique_raw.append(r)

            # DeepSeek 统一解析（把该国所有结果一起发，节省API调用次数）
            if unique_raw:
                leads = self._deepseek_parse(unique_raw[:12], country)
                all_leads.extend(leads)
                print(f"[搜索采集] {country}: {len(leads)} 条有效线索")

        return all_leads


# 单例
google_collector = SerperDeepSeekCollector()
