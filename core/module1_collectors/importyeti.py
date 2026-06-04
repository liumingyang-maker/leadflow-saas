"""
module1_collectors/importyeti.py — 海关数据采集
=================================================
数据来源：ImportYeti.com
  - 免费版：网页直接搜索，返回有限结果
  - 付费版：API 方式，返回完整买家列表 + 联系方式

本模块支持两种模式：
  Mode A (api)    — 付费 API，数据完整，推荐生产使用
  Mode B (scrape) — 免费网页解析，数据有限，适合测试/零成本启动

策略：先用 Mode B 跑通流程，申请到 API 后切换 Mode A，
     代码逻辑不变，只改 config 里的 IMPORTYETI_API_KEY。

使用方式：
    from module1_collectors.importyeti import importer
    leads = importer.fetch_all()          # 遍历所有目标国家+HS编码
    leads = importer.fetch("8407", "Nigeria")  # 指定查询
"""

import time
import random
import json
import hashlib
from pathlib import Path
from typing import Optional

import requests
from compat import logger, cfg


# ──────────────────────────────────────────────────────────
# 请求头伪装（避免被识别为爬虫）
# ──────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.importyeti.com/",
}

# API 端点（付费版）
IMPORTYETI_API_BASE = "https://api.importyeti.com/v1"
# 免费网页搜索端点（逆向分析的内部API，可能随时失效）
IMPORTYETI_WEB_BASE = "https://api.importyeti.com"


class ImportYetiCollector:

    def __init__(self):
        self.api_key = cfg.IMPORTYETI_API_KEY
        self.mode = "api" if self.api_key else "scrape"
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        # 缓存目录：同样的查询不重复请求
        self.cache_dir = cfg.CACHE_DIR / "importyeti"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ──────────────────────────────────────────────────────
    # 缓存机制
    # ──────────────────────────────────────────────────────

    def _cache_key(self, hs_code: str, country: str) -> str:
        raw = f"{hs_code}_{country}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def _read_cache(self, key: str) -> Optional[list]:
        """读缓存（24小时内有效）"""
        path = self.cache_dir / f"{key}.json"
        if not path.exists():
            return None
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if age_hours > 24:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_cache(self, key: str, data: list) -> None:
        path = self.cache_dir / f"{key}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ──────────────────────────────────────────────────────
    # Mode A: 付费 API
    # ──────────────────────────────────────────────────────

    def _fetch_api(self, hs_code: str, country: str) -> list[dict]:
        """
        调用 ImportYeti 付费 API 获取进口商列表。
        文档：https://docs.importyeti.com
        """
        url = f"{IMPORTYETI_API_BASE}/buyers"
        params = {
            "hs_code": hs_code,
            "country": country,
            "limit": 100,
        }
        headers = {**HEADERS, "Authorization": f"Bearer {self.api_key}"}

        try:
            resp = self.session.get(url, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            buyers = data.get("buyers") or data.get("results") or []
            logger.info(f"API返回 {len(buyers)} 条: HS={hs_code} country={country}")
            return [self._normalize_api_record(b, hs_code) for b in buyers]

        except requests.HTTPError as e:
            if e.response.status_code == 429:
                logger.warning("触发限流，等待60秒...")
                time.sleep(60)
                return self._fetch_api(hs_code, country)  # 重试
            elif e.response.status_code == 402:
                logger.error("API额度不足，请升级套餐")
                return []
            else:
                logger.error(f"API请求失败 {e.response.status_code}: {e}")
                return []
        except Exception as e:
            logger.error(f"API请求异常: {e}")
            return []

    def _normalize_api_record(self, raw: dict, hs_code: str) -> dict:
        """API返回字段 → 系统标准格式"""
        return {
            "company_name": raw.get("name") or raw.get("company_name", ""),
            "country": raw.get("country") or raw.get("importer_country", ""),
            "website": raw.get("website") or raw.get("url", ""),
            "email": raw.get("email", ""),
            "phone": raw.get("phone", ""),
            "contact_name": raw.get("contact_name", ""),
            "contact_title": raw.get("contact_title", ""),
            "hs_codes": [hs_code] + (raw.get("hs_codes") or []),
            "import_count_6m": raw.get("shipment_count_6m") or raw.get("count_6m"),
            "last_import_date": raw.get("last_arrival_date") or raw.get("latest_date"),
            "estimated_value_usd": raw.get("estimated_value") or raw.get("total_value_usd"),
            "sources": ["importyeti"],
            "notes": f"ImportYeti API | HS:{hs_code}",
        }

    # ──────────────────────────────────────────────────────
    # Mode B: 免费网页解析（逆向内部 API）
    # ──────────────────────────────────────────────────────

    def _fetch_scrape(self, hs_code: str, country: str) -> list[dict]:
        """
        ImportYeti 免费模式：调用其网页内部 API。
        注意：免费版每个查询只返回前20条，且不含联系方式。
        数据足够用来验证流程，生产环境建议升级付费版。
        """
        # ImportYeti 的内部搜索 API（从网络请求抓包得到）
        url = f"{IMPORTYETI_WEB_BASE}/search"
        params = {
            "searchTerm": f"motorcycle engine {hs_code}",
            "country": country,
        }

        try:
            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            # 解析不同可能的响应结构
            buyers = (
                data.get("suppliers") or
                data.get("buyers") or
                data.get("companies") or
                data.get("results") or
                []
            )

            logger.info(f"网页解析返回 {len(buyers)} 条: {hs_code}/{country}")
            return [self._normalize_scrape_record(b, hs_code, country) for b in buyers]

        except requests.HTTPError as e:
            logger.warning(f"网页请求失败 ({e.response.status_code}): {hs_code}/{country}")
            return []
        except Exception as e:
            logger.warning(f"网页解析异常: {e}")
            return []

    def _normalize_scrape_record(self, raw: dict, hs_code: str, country: str) -> dict:
        """网页抓取字段 → 系统标准格式"""
        # ImportYeti 网页返回字段名可能不同，做多重映射
        name = (
            raw.get("name") or
            raw.get("companyName") or
            raw.get("company_name") or
            raw.get("buyer") or ""
        )
        return {
            "company_name": name,
            "country": raw.get("country") or country,
            "website": raw.get("website") or raw.get("url", ""),
            "email": "",           # 免费版无联系方式
            "hs_codes": [hs_code],
            "import_count_6m": (
                raw.get("shipmentCount") or
                raw.get("shipment_count") or
                raw.get("count")
            ),
            "last_import_date": (
                raw.get("lastDate") or
                raw.get("last_date") or
                raw.get("latestArrival")
            ),
            "estimated_value_usd": raw.get("totalValue") or raw.get("value"),
            "sources": ["importyeti"],
            "notes": f"ImportYeti免费版 | HS:{hs_code}",
        }

    # ──────────────────────────────────────────────────────
    # Mock 模式（无网络时用假数据测试流程）
    # ──────────────────────────────────────────────────────

    def _fetch_mock(self, hs_code: str, country: str) -> list[dict]:
        """
        返回模拟数据，用于：
          1. 开发测试（不消耗API额度）
          2. 演示系统流程
          3. 单元测试
        生产环境绝不应调用此方法。
        """
        mock_companies = {
            "Nigeria": [
                {
                    "company_name": "Lagos Motorcycle Hub Ltd",
                    "country": "Nigeria",
                    "website": "https://lagosmotohub.com",
                    "email": "purchase@lagosmotohub.com",
                    "contact_name": "Chukwuemeka Obi",
                    "contact_title": "Chief Procurement Officer",
                    "hs_codes": [hs_code],
                    "import_count_6m": 14,
                    "last_import_date": "2024-11",
                    "estimated_value_usd": 92000,
                    "sources": ["importyeti"],
                },
                {
                    "company_name": "Abuja Engine Traders",
                    "country": "Nigeria",
                    "website": "https://abujaengine.ng",
                    "email": "info@abujaengine.ng",
                    "hs_codes": [hs_code],
                    "import_count_6m": 7,
                    "last_import_date": "2024-10",
                    "estimated_value_usd": 38000,
                    "sources": ["importyeti"],
                },
                {
                    "company_name": "Delta Moto Supplies",
                    "country": "Nigeria",
                    "hs_codes": [hs_code],
                    "import_count_6m": 3,
                    "last_import_date": "2024-08",
                    "sources": ["importyeti"],
                },
            ],
            "Vietnam": [
                {
                    "company_name": "Hanoi Moto Parts JSC",
                    "country": "Vietnam",
                    "website": "https://hanoiparts.vn",
                    "email": "import@hanoiparts.vn",
                    "contact_name": "Tran Thi Lan",
                    "contact_title": "Import Manager",
                    "hs_codes": [hs_code],
                    "import_count_6m": 9,
                    "last_import_date": "2024-11",
                    "estimated_value_usd": 55000,
                    "sources": ["importyeti"],
                },
                {
                    "company_name": "Saigon Engine Wholesale Co",
                    "country": "Vietnam",
                    "website": "https://saigonengine.vn",
                    "hs_codes": [hs_code],
                    "import_count_6m": 5,
                    "last_import_date": "2024-09",
                    "sources": ["importyeti"],
                },
            ],
            "Pakistan": [
                {
                    "company_name": "Karachi Motor Parts Trading",
                    "country": "Pakistan",
                    "website": "https://karachimoto.pk",
                    "email": "buy@karachimoto.pk",
                    "contact_name": "Muhammad Raza",
                    "contact_title": "Director Imports",
                    "hs_codes": [hs_code],
                    "import_count_6m": 18,
                    "last_import_date": "2024-11",
                    "estimated_value_usd": 125000,
                    "sources": ["importyeti"],
                },
                {
                    "company_name": "Lahore Bike Engine Distributors",
                    "country": "Pakistan",
                    "hs_codes": [hs_code],
                    "import_count_6m": 11,
                    "last_import_date": "2024-10",
                    "sources": ["importyeti"],
                },
            ],
            "Tanzania": [
                {
                    "company_name": "Dar es Salaam Moto Imports",
                    "country": "Tanzania",
                    "website": "https://dsmmoto.co.tz",
                    "email": "procurement@dsmmoto.co.tz",
                    "hs_codes": [hs_code],
                    "import_count_6m": 6,
                    "last_import_date": "2024-10",
                    "estimated_value_usd": 31000,
                    "sources": ["importyeti"],
                },
            ],
            "Indonesia": [
                {
                    "company_name": "PT Mitra Engine Indonesia",
                    "country": "Indonesia",
                    "website": "https://mitraengine.co.id",
                    "email": "import@mitraengine.co.id",
                    "contact_name": "Budi Santoso",
                    "contact_title": "Purchasing Manager",
                    "hs_codes": [hs_code],
                    "import_count_6m": 8,
                    "last_import_date": "2024-11",
                    "estimated_value_usd": 48000,
                    "sources": ["importyeti"],
                },
            ],
        }
        results = mock_companies.get(country, [])
        logger.info(f"[MOCK] 返回 {len(results)} 条: HS={hs_code} country={country}")
        return results

    # ──────────────────────────────────────────────────────
    # 主查询入口
    # ──────────────────────────────────────────────────────

    def fetch(self, hs_code: str, country: str,
              use_cache: bool = True, mock: bool = False) -> list[dict]:
        """
        查询单个 HS码 + 国家 组合的进口商。

        参数：
          hs_code   — 海关HS编码，如 "8407"
          country   — 目标国家，如 "Nigeria"
          use_cache — 是否使用本地缓存（24小时有效）
          mock      — True时返回模拟数据（开发测试用）

        返回标准化的 leads 列表，可直接传给 cleaner.run()
        """
        if mock:
            return self._fetch_mock(hs_code, country)

        # 检查缓存
        cache_key = self._cache_key(hs_code, country)
        if use_cache:
            cached = self._read_cache(cache_key)
            if cached is not None:
                logger.debug(f"命中缓存: {hs_code}/{country} ({len(cached)}条)")
                return cached

        # 根据模式选择采集方式
        if self.mode == "api":
            results = self._fetch_api(hs_code, country)
        else:
            results = self._fetch_scrape(hs_code, country)

        # 写缓存
        if results and use_cache:
            self._write_cache(cache_key, results)

        # 限速
        delay = random.uniform(cfg.REQUEST_DELAY_MIN, cfg.REQUEST_DELAY_MAX)
        time.sleep(delay)

        return results

    def fetch_all(self, mock: bool = False) -> list[dict]:
        """
        遍历目标国家，按每个国家配置的 HS 编码采集。
        规则来自 collection_rules.json（在系统设置里配置）。
        """
        # 构建 (hs_code, country) 任务列表，跳过被排除的国家
        tasks = []
        skipped = []
        for country in cfg.TARGET_COUNTRIES:
            hs_codes = cfg.get_hs_codes_for_country(country)
            if not hs_codes:
                skipped.append(country)
                continue
            for hs_code in hs_codes:
                tasks.append((hs_code, country))

        if skipped:
            logger.info(f"已排除 {len(skipped)} 个国家: {', '.join(skipped)}")
        logger.info(f"开始批量采集: {len(tasks)} 个查询（{len(cfg.TARGET_COUNTRIES) - len(skipped)} 个国家）")

        all_results = []
        for done, (hs_code, country) in enumerate(tasks, 1):
            try:
                results = self.fetch(hs_code, country, mock=mock)
                all_results.extend(results)
                logger.info(f"进度 [{done}/{len(tasks)}] HS={hs_code} {country}: +{len(results)}条")
                if done % 10 == 0:
                    logger.info("批次暂停 5 秒...")
                    time.sleep(5)
            except Exception as e:
                logger.error(f"查询失败: {hs_code}/{country} — {e}")

        logger.success(f"批量采集完成: 共 {len(all_results)} 条原始记录")
        return all_results

    def run_and_clean(self, mock: bool = False) -> dict:
        """
        一键采集 + 清洗 + 写库。
        是给 run.py 和 n8n 调用的便捷入口。
        返回清洗统计。
        """
        raw = self.fetch_all(mock=mock)
        if not raw:
            logger.warning("采集结果为空，跳过清洗")
            return {"input": 0, "db_new": 0}
        stats = cleaner.run(raw, source="importyeti")
        return stats


# 单例
importer = ImportYetiCollector()


# ──────────────────────────────────────────────────────────
# 直接运行 = 用 Mock 数据测试完整采集流程
# ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    from database import db
    db.init()

    print("=" * 55)
    print("测试 1: Mock数据单次查询")
    results = importer.fetch("8407", "Nigeria", mock=True)
    print(f"  返回 {len(results)} 条")
    for r in results:
        print(f"  - {r['company_name']} | 进口{r['import_count_6m']}次")

    print("\n测试 2: Mock数据批量采集 + 清洗写库")
    stats = importer.run_and_clean(mock=True)
    print(f"  清洗统计: {stats}")
    assert stats["db_new"] > 0, "应该有新记录写入"

    print("\n测试 3: 验证写入数据库的数据")
    from database import db
    a_leads, _ = db.search_leads(grade=None, status="new")
    print(f"  数据库中 new 状态: {len(a_leads)} 条")
    for l in a_leads[:3]:
        print(
            f"  - {l['company_name']} ({l['country']}) "
            f"| 进口{l['import_count_6m'] or '?'}次 "
            f"| 邮箱:{l['email'] or '无'}"
        )

    print("\n✅ ImportYeti 采集模块测试通过")
    print("=" * 55)
    print("\n[提示] 实际使用时：")
    print("  1. 在 .env 填入 IMPORTYETI_API_KEY 后自动切换到付费API模式")
    print("  2. 无 API Key 时使用免费网页模式（数据有限）")
    print("  3. 开发测试时传 mock=True 不消耗任何额度")
