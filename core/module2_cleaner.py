"""
module2_cleaner.py — 数据清洗 + 去重 + 标准化
===============================================
职责：把三个数据源（海关/LinkedIn/Google）的原始数据
      清洗成统一格式，去重后批量写入数据库。

核心挑战：同一家公司在三个来源里名字可能写法不同，
          要用模糊匹配识别并合并，而不是重复录入。

使用方式：
    from module2_cleaner import cleaner
    count = cleaner.run(raw_leads)
"""

import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import pycountry
from fuzzywuzzy import fuzz
from log_setup import logger

from config import cfg
from database import db


# ─────────────────────────────────────────────────────────────
# 国家名称映射（常见别名 → 标准英文名）
# 用于处理"尼日利亚"/"NG"/"Nig."等各种写法
# ─────────────────────────────────────────────────────────────
COUNTRY_ALIASES: dict[str, str] = {
    # 非洲
    "nig": "Nigeria", "nig.": "Nigeria", "nigerian": "Nigeria",
    "tan": "Tanzania", "tz": "Tanzania",
    "ken": "Kenya", "ke": "Kenya",
    "gha": "Ghana", "gh": "Ghana",
    "uga": "Uganda", "ug": "Uganda",
    "eth": "Ethiopia", "et": "Ethiopia",
    "moz": "Mozambique", "mz": "Mozambique",
    "cmr": "Cameroon", "cm": "Cameroon",
    "sen": "Senegal", "sn": "Senegal",
    "civ": "Ivory Coast", "ci": "Ivory Coast", "côte d'ivoire": "Ivory Coast",
    # 东南亚
    "viet nam": "Vietnam", "vn": "Vietnam",
    "indo": "Indonesia", "id": "Indonesia",
    "thai": "Thailand", "th": "Thailand",
    "phil": "Philippines", "ph": "Philippines",
    "myan": "Myanmar", "burma": "Myanmar", "mm": "Myanmar",
    # 南亚
    "pak": "Pakistan", "pk": "Pakistan",
    "ind": "India", "in": "India",
    "ban": "Bangladesh", "bd": "Bangladesh",
    "nep": "Nepal", "np": "Nepal",
    # 拉丁美洲
    "bra": "Brazil", "br": "Brazil", "brasil": "Brazil",
    "mex": "Mexico", "mx": "Mexico", "méxico": "Mexico",
    "col": "Colombia", "co": "Colombia",
    "per": "Peru", "pe": "Peru",
    "arg": "Argentina", "ar": "Argentina",
}

# 公司名后缀（标准化时去除）
COMPANY_SUFFIXES: list[str] = [
    r"\bltd\.?\b", r"\bllc\.?\b", r"\binc\.?\b", r"\bcorp\.?\b",
    r"\bco\.?\b", r"\bcompany\b", r"\blimited\b", r"\bcorporation\b",
    r"\benterprises?\b", r"\btrading\b", r"\bgroup\b", r"\bholdings?\b",
    r"\bgmbh\b", r"\bsrl\b", r"\bsa\b", r"\bbv\b", r"\bpvt\b",
    r"\bpty\b", r"\bsdn\b", r"\bbhd\b",
]

# 区域映射
REGION_MAP: dict[str, str] = {
    "Nigeria": "Africa", "Tanzania": "Africa", "Kenya": "Africa",
    "Ghana": "Africa", "Uganda": "Africa", "Ethiopia": "Africa",
    "Mozambique": "Africa", "Cameroon": "Africa", "Senegal": "Africa",
    "Ivory Coast": "Africa",
    "Vietnam": "SEA", "Indonesia": "SEA", "Thailand": "SEA",
    "Philippines": "SEA", "Myanmar": "SEA",
    "Pakistan": "SouthAsia", "India": "SouthAsia",
    "Bangladesh": "SouthAsia", "Nepal": "SouthAsia",
    "Brazil": "LatAm", "Mexico": "LatAm", "Colombia": "LatAm",
    "Peru": "LatAm", "Argentina": "LatAm",
}


class Cleaner:

    # ─────────────────────────────────────────
    # 公司名标准化
    # ─────────────────────────────────────────

    def normalize_company_name(self, name: str) -> str:
        """
        标准化公司名，用于去重比较（不影响存储的显示名）。

        步骤：
        1. 转小写
        2. 去除公司后缀（Ltd/LLC/Inc等）
        3. 去除标点和多余空格
        4. 去除国家名（如 "ABC Motors Nigeria" → "abc motors"）

        例：
          "ABC Motors Ltd." → "abc motors"
          "ABC Motors Nigeria" → "abc motors"
          "A.B.C. Motors, Inc." → "abc motors"
        """
        if not name:
            return ""

        result = name.lower().strip()

        # 去除公司后缀
        for suffix in COMPANY_SUFFIXES:
            result = re.sub(suffix, "", result, flags=re.IGNORECASE)

        # 去除国家名（标准名 + 常见别名，如 "viet nam" / "vietnam"）
        all_country_names = set(c.lower() for c in cfg.TARGET_COUNTRIES)
        # 也加入别名表里的 key（"viet nam", "brasil" 等）
        all_country_names.update(k for k in COUNTRY_ALIASES.keys() if len(k) > 3)
        for country_name in sorted(all_country_names, key=len, reverse=True):
            result = re.sub(r"\b" + re.escape(country_name) + r"\b", "", result)

        # 去除标点（保留字母数字和空格）
        result = re.sub(r"[^\w\s]", " ", result)

        # 合并多余空格
        result = re.sub(r"\s+", " ", result).strip()

        return result

    # ─────────────────────────────────────────
    # 国家名标准化
    # ─────────────────────────────────────────

    def normalize_country(self, raw: str) -> tuple[str, str]:
        """
        将各种写法的国家名统一成标准英文名 + ISO代码。
        返回 (标准名, ISO代码)，无法识别时返回 (原始值, "")

        例：
          "Nigeria" → ("Nigeria", "NG")
          "viet nam" → ("Vietnam", "VN")
          "NIG" → ("Nigeria", "NG")
        """
        if not raw:
            return "", ""

        key = raw.strip().lower()

        # 先查别名表
        if key in COUNTRY_ALIASES:
            standard = COUNTRY_ALIASES[key]
        else:
            # 用 pycountry 查找
            country_obj = (
                pycountry.countries.get(name=raw) or
                pycountry.countries.get(common_name=raw) or
                pycountry.countries.get(alpha_2=raw.upper()) or
                pycountry.countries.get(alpha_3=raw.upper())
            )
            if country_obj:
                standard = country_obj.name
            else:
                # 模糊搜索
                results = pycountry.countries.search_fuzzy(raw)
                standard = results[0].name if results else raw

        # 获取ISO代码（先精确匹配，再模糊搜索，兼容 Ivory Coast 等别名）
        try:
            country_obj = (
                pycountry.countries.get(name=standard) or
                pycountry.countries.get(common_name=standard) or
                pycountry.countries.search_fuzzy(standard)[0]
            )
            iso = country_obj.alpha_2
        except Exception:
            iso = ""

        return standard, iso

    # ─────────────────────────────────────────
    # 网址标准化
    # ─────────────────────────────────────────

    @staticmethod
    def normalize_url(url: str) -> str:
        """确保URL有协议头，去除追踪参数"""
        if not url:
            return ""
        url = url.strip()
        if url and not url.startswith(("http://", "https://")):
            url = "https://" + url
        # 去除常见追踪参数
        url = re.sub(r"\?utm_.*$", "", url)
        return url

    # ─────────────────────────────────────────
    # 邮箱验证
    # ─────────────────────────────────────────

    @staticmethod
    def is_valid_email(email: str) -> bool:
        if not email:
            return False
        pattern = r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
        return bool(re.match(pattern, email.strip()))

    # ─────────────────────────────────────────
    # 日期标准化
    # ─────────────────────────────────────────

    @staticmethod
    def normalize_date(raw: str) -> Optional[str]:
        """
        把各种日期格式转成 YYYY-MM。
        返回 None 如果无法解析。

        支持格式：
          "2024-11"、"2024-11-05"、"Nov 2024"、"11/2024"
        """
        if not raw:
            return None
        raw = str(raw).strip()

        # 已经是 YYYY-MM 格式
        if re.match(r"^\d{4}-\d{2}$", raw):
            return raw

        # YYYY-MM-DD
        m = re.match(r"^(\d{4})-(\d{2})-\d{2}$", raw)
        if m:
            return f"{m.group(1)}-{m.group(2)}"

        # MM/YYYY 或 MM/DD/YYYY
        m = re.match(r"^(\d{1,2})/(\d{4})$", raw)
        if m:
            return f"{m.group(2)}-{int(m.group(1)):02d}"

        # "Nov 2024" / "November 2024"
        months = {
            "jan": "01", "feb": "02", "mar": "03", "apr": "04",
            "may": "05", "jun": "06", "jul": "07", "aug": "08",
            "sep": "09", "oct": "10", "nov": "11", "dec": "12",
        }
        m = re.match(r"^([a-z]{3})[a-z]*\s+(\d{4})$", raw.lower())
        if m and m.group(1) in months:
            return f"{m.group(2)}-{months[m.group(1)]}"

        logger.warning(f"无法解析日期格式: {raw}")
        return None

    # ─────────────────────────────────────────
    # 数据新鲜度计算
    # ─────────────────────────────────────────

    @staticmethod
    def months_since(date_str: str) -> Optional[int]:
        """
        计算距今多少个月。
        date_str 格式: "YYYY-MM"
        """
        if not date_str:
            return None
        try:
            year, month = map(int, date_str.split("-"))
            past = datetime(year, month, 1, tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            delta = (now.year - past.year) * 12 + (now.month - past.month)
            return max(0, delta)
        except Exception:
            return None

    # ─────────────────────────────────────────
    # 区域推断
    # ─────────────────────────────────────────

    @staticmethod
    def get_region(country: str) -> str:
        return REGION_MAP.get(country, "Other")

    # ─────────────────────────────────────────
    # 单条 lead 标准化
    # ─────────────────────────────────────────

    def standardize(self, raw: dict) -> Optional[dict]:
        """
        把任意来源的原始数据转成统一格式。
        如果公司名为空则返回 None（跳过该条）。

        输入字段可以是任何子集，缺失字段填 None。
        """
        company_name = (raw.get("company_name") or "").strip()
        if not company_name:
            logger.debug(f"跳过：公司名为空 → {raw}")
            return None

        # 国家标准化
        country_raw = raw.get("country") or ""
        country, country_iso = self.normalize_country(country_raw)

        # 邮箱验证
        email_raw = (raw.get("email") or "").strip()
        email = email_raw if self.is_valid_email(email_raw) else None

        # 日期标准化
        last_import_date = self.normalize_date(raw.get("last_import_date"))

        # 来源列表（去重）
        sources = raw.get("sources") or []
        if isinstance(sources, str):
            sources = [sources]
        sources = list(set(sources))

        lead = {
            # 标识
            "id": raw.get("id"),                          # 如果已有ID则保留
            # 公司信息
            "company_name": company_name,
            "company_name_norm": self.normalize_company_name(company_name),
            "country": country,
            "country_iso": country_iso,
            "region": self.get_region(country),
            "website": self.normalize_url(raw.get("website")),
            "email": email,
            "phone": (raw.get("phone") or "").strip() or None,
            # 联系人
            "contact_name": (raw.get("contact_name") or "").strip() or None,
            "contact_title": (raw.get("contact_title") or "").strip() or None,
            "linkedin_url": (raw.get("linkedin_url") or "").strip() or None,
            # 采购数据
            "hs_codes": raw.get("hs_codes") or [],
            "import_count_6m": raw.get("import_count_6m"),
            "last_import_date": last_import_date,
            "estimated_value_usd": raw.get("estimated_value_usd"),
            # 元数据
            "sources": sources,
            "status": "new",
            # 评分字段（清洗阶段为空，由 module3 填充）
            "rule_score": None,
            "ai_score_adjustment": None,
            "final_score": None,
            "grade": None,
            "ai_reasoning": None,
            "recommended_approach": None,
            "risk_flags": [],
            "notes": raw.get("notes"),
        }

        return lead

    # ─────────────────────────────────────────
    # 批量去重（同批次内部去重）
    # ─────────────────────────────────────────

    def deduplicate_batch(self, leads: list[dict]) -> tuple[list[dict], int]:
        """
        对同一批次内部的leads进行去重。
        使用模糊匹配：标准化名称相似度 > 85% 且国家相同 → 视为同一家公司。
        合并时保留信息最完整的那条，用另一条补充缺失字段。

        返回 (去重后列表, 被合并的数量)
        """
        if not leads:
            return [], 0

        unique: list[dict] = []
        merged_count = 0

        for lead in leads:
            norm = lead.get("company_name_norm", "")
            country = lead.get("country", "")
            found = False

            for existing in unique:
                # 国家不同 → 肯定不同公司
                if existing.get("country") != country:
                    continue

                # 计算名称相似度
                similarity = fuzz.ratio(norm, existing.get("company_name_norm", ""))
                if similarity >= 85:
                    # 合并：用新数据补充现有数据的空字段
                    self._merge_leads(existing, lead)
                    merged_count += 1
                    found = True
                    logger.debug(
                        f"批次内合并 [{similarity}%]: "
                        f"'{lead['company_name']}' → '{existing['company_name']}'"
                    )
                    break

            if not found:
                unique.append(lead)

        return unique, merged_count

    @staticmethod
    def _merge_leads(base: dict, supplement: dict) -> None:
        """
        用 supplement 补充 base 中的空字段。
        来源列表合并。
        就地修改 base。
        """
        supplement_sources = supplement.get("sources") or []
        for key, value in supplement.items():
            if key == "sources":
                base["sources"] = list(set((base.get("sources") or []) + supplement_sources))
            elif key in ("id", "company_name", "company_name_norm"):
                pass  # 保持 base 的主字段
            elif not base.get(key) and value:
                base[key] = value

        # 进口次数取最大值
        if supplement.get("import_count_6m") and base.get("import_count_6m"):
            base["import_count_6m"] = max(
                base["import_count_6m"], supplement["import_count_6m"]
            )

        # HS 编码合并
        base_codes = set(base.get("hs_codes") or [])
        supp_codes = set(supplement.get("hs_codes") or [])
        base["hs_codes"] = list(base_codes | supp_codes)

    # ─────────────────────────────────────────
    # 主流水线
    # ─────────────────────────────────────────

    def run(self, raw_leads: list[dict], source: str = "unknown") -> dict:
        """
        完整清洗流水线：
          原始数据 → 标准化 → 批次内去重 → 写数据库（跳过已存在）

        返回运行统计：
        {
            "input": 原始条数,
            "invalid": 格式无效跳过数,
            "batch_dupes": 批次内合并数,
            "db_new": 新写入数据库数,
            "db_dupes": 已存在跳过数,
        }
        """
        start = time.time()
        stats = {
            "input": len(raw_leads),
            "invalid": 0,
            "batch_dupes": 0,
            "db_new": 0,
            "db_dupes": 0,
        }

        logger.info(f"清洗开始: 共 {len(raw_leads)} 条原始数据")

        # Step 1: 标准化
        standardized = []
        for raw in raw_leads:
            lead = self.standardize(raw)
            if lead is None:
                stats["invalid"] += 1
            else:
                standardized.append(lead)

        logger.info(f"标准化完成: {len(standardized)} 条有效，{stats['invalid']} 条无效跳过")

        # Step 2: 批次内去重
        deduped, batch_dupes = self.deduplicate_batch(standardized)
        stats["batch_dupes"] = batch_dupes
        logger.info(f"批次内去重: 合并 {batch_dupes} 条，剩余 {len(deduped)} 条")

        # Step 3: 写入数据库（跳过已存在）
        db_new, db_dupes = db.bulk_insert_leads(deduped)
        stats["db_new"] = db_new
        stats["db_dupes"] = db_dupes

        duration = round(time.time() - start, 2)

        # 记录运行日志
        db.log_collection(
            source=source,
            query="cleaning_pipeline",
            results_count=stats["input"],
            new_leads=db_new,
            dupes=batch_dupes + db_dupes,
            errors=stats["invalid"],
            duration_secs=duration,
        )

        logger.success(
            f"清洗完成 ({duration}s): "
            f"输入{stats['input']} → "
            f"新增{db_new} | 合并{batch_dupes} | 跳过{db_dupes} | 无效{stats['invalid']}"
        )
        return stats


# 单例
cleaner = Cleaner()


# ─────────────────────────────────────────
# 直接运行此文件 = 清洗测试
# ─────────────────────────────────────────
if __name__ == "__main__":
    import sys
    import logging
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

    db.init()

    print("=" * 55)
    print("测试 1: 国家名标准化")
    test_countries = [
        ("Nigeria", ("Nigeria", "NG")),
        ("viet nam", ("Vietnam", "VN")),
        ("NIG", ("Nigeria", "NG")),
        ("Côte d'Ivoire", ("Ivory Coast", "CI")),
        ("PK", ("Pakistan", "PK")),
    ]
    for raw, expected in test_countries:
        result = cleaner.normalize_country(raw)
        status = "✅" if result[1] == expected[1] else "❌"
        print(f"  {status} '{raw}' → {result}")

    print("\n测试 2: 公司名标准化")
    test_names = [
        "ABC Motors Ltd.",
        "ABC Motors Nigeria",
        "A.B.C. Motors, Inc.",
        "abc motors   ",
    ]
    for name in test_names:
        print(f"  '{name}' → '{cleaner.normalize_company_name(name)}'")

    print("\n测试 3: 日期标准化")
    test_dates = ["2024-11", "2024-11-05", "11/2024", "Nov 2024", "November 2024"]
    for d in test_dates:
        print(f"  '{d}' → '{cleaner.normalize_date(d)}'")

    print("\n测试 4: 完整清洗流水线（含去重）")
    raw_data = [
        # 正常条目
        {
            "company_name": "Delta Moto Vietnam",
            "country": "Vietnam",
            "email": "purchase@deltamoto.vn",
            "hs_codes": ["8407"],
            "import_count_6m": 6,
            "last_import_date": "2024-10",
            "sources": ["importyeti"],
        },
        # 名字略有不同的同一家公司（应被合并）
        {
            "company_name": "Delta Moto Viet Nam",
            "country": "Vietnam",
            "contact_name": "Nguyen Van A",
            "sources": ["linkedin"],
        },
        # 公司名为空（应被跳过）
        {
            "company_name": "",
            "country": "Nigeria",
            "sources": ["google"],
        },
        # 不同国家，不合并
        {
            "company_name": "Global Engine Imports",
            "country": "Nigeria",
            "email": "info@globalengine.ng",
            "import_count_6m": 12,
            "last_import_date": "Nov 2024",
            "sources": ["importyeti"],
        },
    ]

    stats = cleaner.run(raw_data, source="test")
    print(f"\n  统计: {stats}")
    assert stats["input"] == 4
    assert stats["invalid"] == 1   # 空公司名
    assert stats["batch_dupes"] == 1  # Delta Moto被合并
    assert stats["db_new"] == 2    # 2条真正写入（第一次运行）
    print("  ✅ 流水线测试通过")

    print("\n测试 5: 验证合并后联系人信息被补充")
    leads, _ = db.search_leads(keyword="Delta Moto")
    if leads:
        lead = leads[0]
        print(f"  公司: {lead['company_name']}")
        print(f"  邮箱: {lead['email']}")         # 来自第1条
        print(f"  联系人: {lead['contact_name']}") # 来自第2条（合并）
        print(f"  来源: {lead['sources']}")        # 应包含两个来源
        assert lead["contact_name"] == "Nguyen Van A"
        assert set(lead["sources"]) == {"importyeti", "linkedin"}
        print("  ✅ 合并数据完整")

    print("\n✅ 所有清洗测试通过")
    print("=" * 55)
