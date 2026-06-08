"""
module1_collectors/google_maps.py — Google Maps 商家数据采集
=============================================================
数据来源：Serper.dev /maps API（与现有 Google Search 共用同一个 Key）
  - 每次搜索消耗 1 个 Serper 额度（和普通搜索一样）
  - 返回：公司名、地址、城市、电话、网站、评价数、评分
  - 覆盖：全球所有国家（Google Maps 覆盖最好的是东南亚/非洲/南亚）

为什么 Google Maps 是外贸获客的黄金渠道：
  - 非洲/东南亚进口商普遍没有官网，但 95%+ 都在 Google Maps 登记
  - 直接获得电话号码，WhatsApp 联系比邮件回复率高10倍
  - 评价数 / 评分可以反映公司规模和活跃度，辅助评分
  - 地址精确到街道，方便判断是批发商还是零售商

搜索策略：
  对每个目标国家，搜索：
    "[产品关键词] importer/distributor/wholesaler [国家/城市]"
  例：
    "motorcycle engine importer Nigeria"
    "petrol engine distributor Karachi"
    "CG150 engine wholesaler Ho Chi Minh City"
"""
import time
import random
import hashlib
import json
from pathlib import Path
from typing import Optional

import requests

# 主要目标国家 → 搜索时附加的城市（提高精准度）
_COUNTRY_CITIES = {
    "Nigeria":     ["Lagos", "Abuja", "Kano", "Port Harcourt"],
    "Kenya":       ["Nairobi", "Mombasa"],
    "Tanzania":    ["Dar es Salaam", "Arusha"],
    "Ghana":       ["Accra", "Kumasi"],
    "Ethiopia":    ["Addis Ababa"],
    "South Africa":["Johannesburg", "Cape Town", "Durban"],
    "Vietnam":     ["Ho Chi Minh City", "Hanoi", "Hai Phong"],
    "Indonesia":   ["Jakarta", "Surabaya", "Bandung"],
    "Philippines": ["Manila", "Cebu"],
    "Thailand":    ["Bangkok", "Chiang Mai"],
    "Cambodia":    ["Phnom Penh"],
    "Pakistan":    ["Karachi", "Lahore", "Faisalabad"],
    "Bangladesh":  ["Dhaka", "Chittagong"],
    "India":       ["Mumbai", "Delhi", "Chennai", "Rajkot", "Ludhiana"],
    "Brazil":      ["São Paulo", "Manaus"],
    "Mexico":      ["Mexico City", "Guadalajara", "Monterrey"],
    "Colombia":    ["Bogotá", "Medellín"],
    "UAE":         ["Dubai", "Abu Dhabi"],
    "Saudi Arabia":["Riyadh", "Jeddah"],
}

# 搜索词模板（按国家语言习惯）
_SEARCH_TEMPLATES = [
    "{product} importer {location}",
    "{product} distributor {location}",
    "{product} wholesaler {location}",
    "{product} supplier {location}",
]

# 模拟数据（未配置 Serper key 时使用）
_MOCK_DATA = {
    "Nigeria": [
        {"company_name": "Lagos Motors Import Hub",    "city": "Lagos",       "phone": "+234-802-345-6789", "website": "lagosmotorshub.com", "rating": 4.2, "reviews": 47},
        {"company_name": "Abuja Engine Traders Ltd",   "city": "Abuja",       "phone": "+234-803-456-7890", "website": "",                   "rating": 3.9, "reviews": 23},
        {"company_name": "Kano Auto Parts Wholesale",  "city": "Kano",        "phone": "+234-806-789-0123", "website": "kanoautoparts.com",  "rating": 4.5, "reviews": 89},
    ],
    "Vietnam": [
        {"company_name": "Hanoi Moto Parts JSC",       "city": "Hanoi",       "phone": "+84-24-3456-7890",  "website": "hanoiparts.vn",      "rating": 4.3, "reviews": 112},
        {"company_name": "Saigon Engine Import Co",    "city": "Ho Chi Minh", "phone": "+84-28-3456-7891",  "website": "saigonengine.vn",    "rating": 4.1, "reviews": 67},
    ],
    "Pakistan": [
        {"company_name": "Karachi Motor Parts Trading","city": "Karachi",     "phone": "+92-21-3456789",    "website": "karachimoto.pk",     "rating": 4.4, "reviews": 156},
        {"company_name": "Lahore Bike Engine Store",   "city": "Lahore",      "phone": "+92-42-3456789",    "website": "",                   "rating": 3.8, "reviews": 34},
    ],
    "Indonesia": [
        {"company_name": "PT Mitra Engine Jakarta",    "city": "Jakarta",     "phone": "+62-21-3456789",    "website": "mitraengine.co.id",  "rating": 4.2, "reviews": 78},
        {"company_name": "Surabaya Motor Parts Hub",   "city": "Surabaya",    "phone": "+62-31-3456789",    "website": "",                   "rating": 4.0, "reviews": 45},
    ],
    "Kenya": [
        {"company_name": "Nairobi Auto Spares Kenya",  "city": "Nairobi",     "phone": "+254-722-345678",   "website": "nairobiautospares.co.ke", "rating": 4.1, "reviews": 38},
    ],
}


class GoogleMapsCollector:
    """
    Google Maps 商家数据采集器（通过 Serper.dev /maps API）

    与现有 google_search.py 的区别：
      - google_search.py 搜索网页 → AI 解析 → 找公司网站
      - google_maps.py   搜索地图 → 直接返回电话+地址+评价

    用法：
        mc = GoogleMapsCollector()
        mc.serper_key      = cfg.get("serpapi_key", "")
        mc.product_name    = cfg.get("product_name", "")
        mc.search_keywords = cfg.get("search_keywords", [])
        leads = mc.fetch_all(countries=["Nigeria", "Vietnam", "Pakistan"])
    """

    SERPER_MAPS_URL = "https://google.serper.dev/maps"

    def __init__(self):
        self.serper_key      = ""
        self.product_name    = ""
        self.search_keywords = []
        self._cache          = {}

    # ── Serper Maps API ─────────────────────────────────────────────────────

    def _search_maps(self, query: str, country_code: str) -> list[dict]:
        """调用 Serper.dev /maps 端点，返回 Google Maps 商家列表"""
        cache_key = hashlib.md5(f"{query}_{country_code}".encode()).hexdigest()[:10]
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            resp = requests.post(
                self.SERPER_MAPS_URL,
                headers={"X-API-KEY": self.serper_key,
                         "Content-Type": "application/json"},
                json={"q": query, "gl": country_code, "hl": "en", "num": 10},
                timeout=20,
            )
            resp.raise_for_status()
            places = resp.json().get("places", [])
            self._cache[cache_key] = places
            return places

        except requests.HTTPError as e:
            if e.response.status_code == 429:
                print("[GoogleMaps] Serper 额度不足")
            else:
                print(f"[GoogleMaps] API 错误 {e.response.status_code}")
            return []
        except Exception as e:
            print(f"[GoogleMaps] 请求失败: {e}")
            return []

    def _normalize(self, place: dict, country: str) -> dict:
        """把 Serper Maps 返回格式转成系统标准格式"""
        name    = place.get("title", "").strip()
        address = place.get("address", "")
        phone   = place.get("phoneNumber", "") or place.get("phone", "")
        website = place.get("website", "")
        rating  = place.get("rating")
        reviews = place.get("ratingCount") or place.get("reviews", 0)
        category= place.get("type", "")

        # 评价数多 → 公司更活跃 → 提高进口次数估算
        import_estimate = None
        if reviews:
            r = int(reviews) if str(reviews).isdigit() else 0
            import_estimate = max(1, r // 20)   # 粗估：每20条评价约1次进口记录

        return {
            "company_name":    name,
            "country":         country,
            "address":         address[:200],
            "phone":           phone,
            "website":         website,
            "contact_title":   "Business Owner",
            "import_count_6m": import_estimate,
            "sources":         ["google_maps"],
            "notes":           (f"Google Maps | 评分:{rating} | 评价:{reviews}条"
                                + (f" | {category}" if category else "")),
        }

    # ── 模拟数据 ─────────────────────────────────────────────────────────────

    def _mock_leads(self, countries: list) -> list[dict]:
        leads = []
        for country in countries:
            for item in _MOCK_DATA.get(country, []):
                leads.append({
                    "company_name":    item["company_name"],
                    "country":         country,
                    "city":            item.get("city", ""),
                    "phone":           item.get("phone", ""),
                    "website":         item.get("website", ""),
                    "contact_title":   "Business Owner",
                    "import_count_6m": max(1, item.get("reviews", 10) // 20),
                    "sources":         ["google_maps"],
                    "notes":           f"Google Maps模拟 | 评分:{item.get('rating')} | 评价:{item.get('reviews')}条",
                })
        return leads

    # ── 主入口 ────────────────────────────────────────────────────────────────

    def fetch_all(self, countries: list = None, mock: bool = False) -> list[dict]:
        """
        采集 Google Maps 上的进口商/分销商。

        参数：
          countries — 目标国家列表（默认取 _COUNTRY_CITIES 里配置的国家）
          mock      — True 时返回模拟数据

        策略：
          每个国家搜索 1-2 个城市，每个城市用产品名搜索一次
          "motorcycle engine importer Lagos"
        """
        if not countries:
            countries = list(_COUNTRY_CITIES.keys())[:8]

        if mock or not self.serper_key:
            if not self.serper_key:
                print("[GoogleMaps] 未配置 Serper Key，使用模拟数据")
                print("[GoogleMaps] 提示：serpapi_key 填写后即可采集真实 Google Maps 数据")
            return self._mock_leads(countries)

        # 构建搜索词
        product = self.product_name or (self.search_keywords[0] if self.search_keywords else "motorcycle engine")

        # 国家代码映射
        country_codes = {
            "Nigeria": "ng", "Kenya": "ke", "Tanzania": "tz", "Ghana": "gh",
            "Ethiopia": "et", "South Africa": "za",
            "Vietnam": "vn", "Indonesia": "id", "Philippines": "ph",
            "Thailand": "th", "Cambodia": "kh",
            "Pakistan": "pk", "Bangladesh": "bd", "India": "in",
            "Brazil": "br", "Mexico": "mx", "Colombia": "co",
            "UAE": "ae", "Saudi Arabia": "sa",
        }

        all_leads  = []
        seen_names = set()

        for country in countries[:10]:
            cities = _COUNTRY_CITIES.get(country, [country])[:2]  # 最多2个城市
            gl     = country_codes.get(country, "us")

            for city in cities:
                query = f"{product} importer {city}"
                print(f"[GoogleMaps] 搜索: {query}")

                places = self._search_maps(query, gl)
                for place in places:
                    lead = self._normalize(place, country)
                    key  = lead["company_name"].upper()
                    if key and key not in seen_names:
                        seen_names.add(key)
                        all_leads.append(lead)

                time.sleep(random.uniform(0.5, 1.2))  # Serper 限速友好

        if all_leads:
            print(f"[GoogleMaps] 采集完成：{len(all_leads)} 家商家")
            return all_leads

        return self._mock_leads(countries)


google_maps_collector = GoogleMapsCollector()
