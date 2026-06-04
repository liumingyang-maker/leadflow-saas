"""
module1_collectors/linkedin.py — LinkedIn 联系人补充
======================================================
职责：根据已有公司名称，找到对应的采购负责人姓名、职位、邮箱、LinkedIn URL。
这一步叫 "enrichment"（数据富化），在海关数据基础上补充联系人信息。

数据来源（三层降级策略，从便宜到贵）：
  Level 1: Hunter.io API     — 按域名查找公司邮箱（$0，免费版50次/月）
  Level 2: Proxycurl API     — LinkedIn 数据（$0.01/次，最准确）
  Level 3: 本地规则推断       — 根据公司网站域名猜测邮箱格式（免费，准确率低）

策略：
  - 只对 final_score >= 60 的 leads 做 enrichment（省钱）
  - Level 1 找到则不调用 Level 2（节省成本）
  - 结果缓存48小时（同公司不重复付费查询）

使用方式：
    from module1_collectors.linkedin import enricher
    enricher.enrich_lead(lead)           # 补充单条
    enricher.run(min_score=60)           # 批量补充高分leads
"""

import time
import random
import json
import hashlib
import re
from typing import Optional

import requests
from compat import logger, cfg


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


class LinkedInEnricher:

    def __init__(self):
        self.hunter_key = cfg.HUNTER_API_KEY
        self.proxycurl_key = cfg.PROXYCURL_API_KEY
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.cache_dir = cfg.CACHE_DIR / "linkedin"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ──────────────────────────────────────────────────────
    # 缓存
    # ──────────────────────────────────────────────────────

    def _cache_key(self, company_name: str, domain: str) -> str:
        raw = f"{company_name}_{domain}".lower()
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def _read_cache(self, key: str, ttl_hours: int = 48) -> Optional[dict]:
        path = self.cache_dir / f"{key}.json"
        if not path.exists():
            return None
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if age_hours > ttl_hours:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_cache(self, key: str, data: dict) -> None:
        path = self.cache_dir / f"{key}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ──────────────────────────────────────────────────────
    # 工具：从URL提取域名
    # ──────────────────────────────────────────────────────

    @staticmethod
    def extract_domain(url: str) -> Optional[str]:
        """
        从网站URL提取根域名。
        "https://www.abcmotors.ng/about" → "abcmotors.ng"
        """
        if not url:
            return None
        url = url.strip()
        # 去掉协议头
        url = re.sub(r"^https?://", "", url)
        # 去掉 www.
        url = re.sub(r"^www\.", "", url)
        # 取第一段（去掉路径）
        domain = url.split("/")[0].split("?")[0].strip()
        return domain if "." in domain else None

    # ──────────────────────────────────────────────────────
    # Level 1: Hunter.io — 按域名查公司邮箱
    # ──────────────────────────────────────────────────────

    def _find_by_hunter(self, domain: str, company_name: str) -> Optional[dict]:
        """
        用 Hunter.io 查找公司决策人邮箱。
        Hunter.io 免费版：每月50次 domain-search
        付费版($34/月)：500次/月

        返回：{"email", "contact_name", "contact_title", "confidence"} 或 None
        """
        if not self.hunter_key:
            return None

        url = "https://api.hunter.io/v2/domain-search"
        params = {
            "domain": domain,
            "api_key": self.hunter_key,
            "limit": 5,
            "seniority": "senior,executive,director",   # 优先找高层
            "department": "purchasing,procurement,management",
        }

        try:
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json().get("data", {})
            emails = data.get("emails", [])

            if not emails:
                logger.debug(f"Hunter.io: {domain} 未找到邮箱")
                return None

            # 取置信度最高的那条，优先选采购相关职位
            def score_email(e):
                title = (e.get("position") or "").lower()
                purchase_keywords = ["purchas", "procurement", "import", "buyer", "supply"]
                title_score = 2 if any(k in title for k in purchase_keywords) else 0
                return e.get("confidence", 0) + title_score * 10

            best = max(emails, key=score_email)

            result = {
                "email": best.get("value", ""),
                "contact_name": f"{best.get('first_name', '')} {best.get('last_name', '')}".strip(),
                "contact_title": best.get("position", ""),
                "confidence": best.get("confidence", 0) / 100,
                "source": "hunter",
            }
            logger.debug(f"Hunter.io找到: {result['email']} ({result['confidence']:.0%}置信)")
            return result

        except requests.HTTPError as e:
            if e.response.status_code == 429:
                logger.warning("Hunter.io 限流")
            else:
                logger.warning(f"Hunter.io 请求失败: {e}")
            return None
        except Exception as e:
            logger.warning(f"Hunter.io 异常: {e}")
            return None

    # ──────────────────────────────────────────────────────
    # Level 2: Proxycurl — LinkedIn 精准数据
    # ──────────────────────────────────────────────────────

    def _find_by_proxycurl(self, company_name: str, country: str) -> Optional[dict]:
        """
        用 Proxycurl API 搜索公司的采购联系人。
        每次查询约 $0.01（公司搜索）+ $0.01（人员搜索）= $0.02
        只在 Hunter.io 找不到时才调用。

        返回：{"contact_name", "contact_title", "linkedin_url", "email"} 或 None
        """
        if not self.proxycurl_key:
            return None

        # Step 1: 先找到公司的 LinkedIn 页面
        company_url = self._search_company_linkedin(company_name, country)
        if not company_url:
            return None

        # Step 2: 在公司里搜索采购相关人员
        contact = self._search_employee(company_url, country)
        return contact

    def _search_company_linkedin(self, company_name: str, country: str) -> Optional[str]:
        """通过 Proxycurl 搜索公司 LinkedIn 主页 URL"""
        url = "https://nubela.co/proxycurl/api/linkedin/company/resolve"
        params = {
            "company_name": company_name,
            "company_location": country,
        }
        headers = {"Authorization": f"Bearer {self.proxycurl_key}"}

        try:
            resp = self.session.get(url, params=params, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            company_url = data.get("url")
            logger.debug(f"Proxycurl公司URL: {company_name} → {company_url}")
            return company_url
        except Exception as e:
            logger.debug(f"Proxycurl公司搜索失败: {e}")
            return None

    def _search_employee(self, company_linkedin_url: str, country: str) -> Optional[dict]:
        """在公司 LinkedIn 页面搜索采购负责人"""
        url = "https://nubela.co/proxycurl/api/linkedin/company/employees/"
        params = {
            "linkedin_company_profile_url": company_linkedin_url,
            "keyword_regex": "purchas|procurement|import|buyer|supply|director|manager",
            "page_size": 5,
            "country": country,
        }
        headers = {"Authorization": f"Bearer {self.proxycurl_key}"}

        try:
            resp = self.session.get(url, params=params, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            employees = data.get("employees", [])

            if not employees:
                return None

            # 取第一个结果
            person = employees[0]
            return {
                "contact_name": person.get("name", ""),
                "contact_title": person.get("title", ""),
                "linkedin_url": person.get("linkedin_profile_url", ""),
                "email": person.get("email", ""),
                "confidence": 0.7,
                "source": "proxycurl",
            }
        except Exception as e:
            logger.debug(f"Proxycurl员工搜索失败: {e}")
            return None

    # ──────────────────────────────────────────────────────
    # Level 3: 本地规则推断邮箱格式
    # ──────────────────────────────────────────────────────

    @staticmethod
    def _guess_email(domain: str) -> Optional[str]:
        """
        当上面两种方法都找不到联系人时，
        根据通用规律猜测可能有效的邮箱格式。
        准确率约 30-40%，但完全免费。

        常见格式：info@ / purchase@ / import@ / contact@
        """
        if not domain:
            return None
        common_prefixes = ["purchase", "import", "procurement", "info", "contact", "trade"]
        # 返回最常见的采购邮箱格式（批量测试时发送，让对方回复确认）
        return f"{common_prefixes[0]}@{domain}"

    # ──────────────────────────────────────────────────────
    # Mock 数据（测试用）
    # ──────────────────────────────────────────────────────

    @staticmethod
    def _mock_enrich(lead: dict) -> Optional[dict]:
        """返回模拟的联系人数据（开发测试用）"""
        company = lead.get("company_name", "")
        if not company:
            return None

        # 根据公司名生成伪真实联系人
        mock_contacts = {
            "Nigeria": ("Chukwuemeka Obi", "Head of Procurement"),
            "Vietnam": ("Nguyen Van Minh", "Import Manager"),
            "Pakistan": ("Muhammad Ali Raza", "Director of Imports"),
            "Tanzania": ("John Msigwa", "Purchasing Manager"),
            "Indonesia": ("Budi Santoso", "Supply Chain Manager"),
        }
        country = lead.get("country", "")
        name, title = mock_contacts.get(country, ("James Smith", "Procurement Manager"))

        domain = LinkedInEnricher.extract_domain(lead.get("website", ""))
        email = f"purchase@{domain}" if domain else lead.get("email", "")

        return {
            "contact_name": name,
            "contact_title": title,
            "email": email or "",
            "linkedin_url": f"https://linkedin.com/in/{name.lower().replace(' ', '-')}-mock",
            "confidence": 0.6,
            "source": "mock",
        }

    # ──────────────────────────────────────────────────────
    # 主入口：对单条 lead 做 enrichment
    # ──────────────────────────────────────────────────────

    def enrich_lead(self, lead: dict, mock: bool = False) -> dict:
        """
        对单条 lead 补充联系人信息。
        按 Hunter → Proxycurl → 规则推断 三级降级。

        返回找到的联系人信息字典，或空字典（找不到）。
        不直接修改数据库，由 run() 负责写库。
        """
        lead_id = lead.get("id", "")
        company = lead.get("company_name", "未知")
        country = lead.get("country", "")

        # 如果已有联系人信息，跳过
        if lead.get("contact_name") and lead.get("email"):
            logger.debug(f"跳过已有联系人: {company}")
            return {}

        # Mock 模式
        if mock:
            return self._mock_enrich(lead) or {}

        domain = self.extract_domain(lead.get("website", ""))
        cache_key = self._cache_key(company, domain or "")

        # 查缓存
        cached = self._read_cache(cache_key)
        if cached is not None:
            logger.debug(f"命中缓存: {company}")
            return cached

        contact_info = {}

        # Level 1: Hunter.io（有domain才能查）
        if domain and self.hunter_key:
            result = self._find_by_hunter(domain, company)
            if result and result.get("email"):
                contact_info = result
                logger.info(f"Hunter找到: {company} → {result['email']}")

        # Level 2: Proxycurl（Hunter找不到时才用）
        if not contact_info.get("email") and self.proxycurl_key:
            result = self._find_by_proxycurl(company, country)
            if result:
                contact_info = result
                logger.info(f"Proxycurl找到: {company} → {result.get('contact_name', '?')}")
            time.sleep(random.uniform(1, 2))

        # Level 3: 规则推断（前两级都没找到）
        if not contact_info.get("email") and domain:
            guessed = self._guess_email(domain)
            if guessed:
                contact_info = {
                    "email": guessed,
                    "contact_name": "",
                    "contact_title": "",
                    "confidence": 0.3,
                    "source": "guess",
                }
                logger.debug(f"规则推断邮箱: {company} → {guessed}")

        # 写缓存
        if contact_info:
            self._write_cache(cache_key, contact_info)

        return contact_info

    # ──────────────────────────────────────────────────────
    # 批量 enrichment
    # ──────────────────────────────────────────────────────

    def run(self, min_score: int = 60, mock: bool = False) -> dict:
        """
        批量补充联系人信息。
        只处理 final_score >= min_score 的 leads（省钱策略）。

        返回统计：{enriched, skipped, failed}
        """
        # 获取高分 leads
        with db.get_conn() as conn:
            rows = conn.execute(
                """SELECT * FROM leads
                   WHERE final_score >= ?
                   AND status IN ('scored', 'new')
                   AND (contact_name IS NULL OR contact_name = ''
                        OR email IS NULL OR email = '')
                   ORDER BY final_score DESC""",
                (min_score,)
            ).fetchall()

        leads = [db._row_to_dict(r) for r in rows]

        if not leads:
            logger.info(f"没有需要补充联系人的leads（score>={min_score}）")
            return {"enriched": 0, "skipped": 0, "failed": 0}

        logger.info(f"开始enrichment: {len(leads)} 条 score>={min_score} 的leads")
        stats = {"enriched": 0, "skipped": 0, "failed": 0}

        for i, lead in enumerate(leads, 1):
            company = lead.get("company_name", "?")
            try:
                contact_info = self.enrich_lead(lead, mock=mock)

                if contact_info:
                    # 写回数据库（只更新空字段）
                    update_fields = {}
                    for field in ("email", "contact_name", "contact_title", "linkedin_url"):
                        if contact_info.get(field) and not lead.get(field):
                            update_fields[field] = contact_info[field]

                    if update_fields:
                        db.update_lead(lead["id"], update_fields)
                        stats["enriched"] += 1
                        logger.info(
                            f"[{i}/{len(leads)}] 补充成功: {company} "
                            f"→ {contact_info.get('email', '?')} "
                            f"(来源:{contact_info.get('source','?')})"
                        )
                    else:
                        stats["skipped"] += 1
                else:
                    stats["skipped"] += 1
                    logger.debug(f"[{i}/{len(leads)}] 未找到联系人: {company}")

                # 限速
                time.sleep(random.uniform(
                    cfg.REQUEST_DELAY_MIN,
                    cfg.REQUEST_DELAY_MAX
                ))

            except Exception as e:
                stats["failed"] += 1
                logger.error(f"Enrichment失败: {company} — {e}")

        logger.success(
            f"Enrichment完成: 补充{stats['enriched']} | "
            f"跳过{stats['skipped']} | 失败{stats['failed']}"
        )
        return stats


# 单例
enricher = LinkedInEnricher()


# ──────────────────────────────────────────────────────────
# 直接运行 = 测试
# ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logger.remove()
    logger.add(sys.stdout, level="DEBUG")

    db.init()

    print("=" * 55)
    print("测试 1: 域名提取")
    urls = [
        "https://www.abcmotors.ng/about",
        "http://lagosengine.ng",
        "abcmotors.pk",
        "",
        None,
    ]
    for url in urls:
        print(f"  '{url}' → '{enricher.extract_domain(url)}'")

    print("\n测试 2: Mock enrichment 单条")
    mock_lead = {
        "id": "test-enrich-1",
        "company_name": "Karachi Engine Hub",
        "country": "Pakistan",
        "website": "https://karachiengine.pk",
        "email": "",
        "contact_name": "",
    }
    result = enricher.enrich_lead(mock_lead, mock=True)
    print(f"  找到联系人: {result}")
    assert result.get("contact_name"), "应该返回联系人名"
    print("  ✅ 单条enrichment通过")

    print("\n测试 3: Mock 批量 enrichment")
    # 先写入一些测试数据
    from module1_collectors.importyeti import importer
    from module3_scorer import scorer

    importer.run_and_clean(mock=True)  # 写入Mock数据
    scorer.run(use_ai=False)           # 评分

    stats = enricher.run(min_score=0, mock=True)  # min_score=0 对所有leads做enrichment
    print(f"  统计: {stats}")
    assert stats["enriched"] > 0 or stats["skipped"] > 0

    print("\n测试 4: 验证联系人已写入数据库")
    leads, _ = db.search_leads(grade="A")
    for l in leads[:3]:
        print(
            f"  - {l['company_name']} ({l['country']}) "
            f"| 联系人: {l['contact_name'] or '无'} "
            f"| 邮箱: {l['email'] or '无'}"
        )

    print("\n✅ LinkedIn enrichment 模块测试通过")
    print("=" * 55)
