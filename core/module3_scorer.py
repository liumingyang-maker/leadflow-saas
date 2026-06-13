"""
module3_scorer.py — AI 评分层
==============================
职责：对数据库里 status='new' 的 leads 进行评分，
      写回 rule_score / final_score / grade / ai_reasoning 等字段。

两阶段评分策略（省钱 + 精准）：
  阶段1：规则打分（所有leads，零成本）
  阶段2：Claude AI 深度分析（只对规则分 >= 阈值的leads）

使用方式：
    from module3_scorer import scorer
    scorer.run()                    # 对所有new状态的leads评分
    scorer.run(lead_ids=["xxx"])    # 对指定leads评分
"""

import json
import time
import random
from typing import Optional

try:
    import anthropic
except ImportError:
    anthropic = None
from compat import logger, cfg


class Scorer:

    def __init__(self, db_path: str = None):
        # Claude 客户端（懒加载，没有API key时不报错）
        self._client: Optional[anthropic.Anthropic] = None
        # 租户数据库路径（SaaS 多租户模式必传；不传则回退旧版全局 db，仅供测试）
        self._db_path = db_path

    def _resolve_db(self):
        """按 db_path 解析租户专属数据库实例（与 module2_cleaner 同款约定）。"""
        if self._db_path:
            from database import Database
            _db = Database(db_path=self._db_path)
            _db.init()
            return _db
        try:
            return db   # 旧版单用户全局对象（向后兼容，仅 __main__ 测试用）
        except NameError:
            raise RuntimeError("Scorer 需要传入 db_path 参数（SaaS 多租户模式）")

    @property
    def client(self) -> Optional[anthropic.Anthropic]:
        if self._client is None and cfg.CLAUDE_API_KEY:
            self._client = anthropic.Anthropic(api_key=cfg.CLAUDE_API_KEY)
        return self._client

    # ─────────────────────────────────────────
    # 阶段 1：规则打分
    # ─────────────────────────────────────────

    def score_import_frequency(self, count: Optional[int]) -> int:
        """
        进口频率评分（30分）
        近6个月进口次数越多分越高。
        """
        if count is None:
            return 5   # 无数据给少量分，不完全归零
        # 兼容两套键名：旧版 config 用 "10+/5-9/1-4/0"，compat._SafeCfg 用 very_high/high/medium/low/none
        rules = cfg.SCORE_RULES["import_frequency"]
        if count >= 10:
            return rules.get("10+", rules.get("very_high", 20))
        elif count >= 5:
            return rules.get("5-9", rules.get("high", 15))
        elif count >= 1:
            return rules.get("1-4", rules.get("medium", 10))
        return rules.get("0", rules.get("none", 0))

    def score_product_match(self, hs_codes: Optional[list]) -> int:
        """
        产品匹配度（25分）
        HS编码和摩托车发动机的相关程度。
        """
        if not hs_codes:
            return cfg.SCORE_RULES["product_match"]["unclear"]

        codes = [str(c).strip()[:4] for c in hs_codes]  # 取前4位

        # 完全匹配：发动机主HS码
        engine_codes = {"8407", "8408"}
        if any(c in engine_codes for c in codes):
            return cfg.SCORE_RULES["product_match"]["exact"]

        # 相关品类：摩托车零件/整车
        related_codes = {"8714", "8711", "8712", "8413", "8481"}
        if any(c in related_codes for c in codes):
            return cfg.SCORE_RULES["product_match"]["related"]

        # 描述不明确
        return cfg.SCORE_RULES["product_match"]["unclear"]

    def score_market_priority(self, country: Optional[str]) -> int:
        """
        目标市场优先级（20分）
        非洲/东南亚是最优先市场。
        """
        if not country:
            return 5

        tier = cfg.get_country_tier(country)
        tier_scores = {"tier1": 20, "tier2": 15, "tier3": 10}
        return tier_scores.get(tier, 5)

    def score_company_verify(self, lead: dict) -> int:
        """
        公司真实性验证（15分）
        有官网+LinkedIn说明是真实运营的公司。
        """
        score = 0
        if lead.get("website"):
            score += 8
        if lead.get("linkedin_url"):
            score += 4
        if lead.get("email"):
            score += 2
        if lead.get("contact_name"):
            score += 1
        return min(score, 15)   # 上限15分

    def score_data_recency(self, last_import_date: Optional[str]) -> int:
        """
        数据新鲜度（10分）
        进口记录越近越好。
        """
        from module2_cleaner import cleaner
        months = cleaner.months_since(last_import_date)

        if months is None:
            return 3   # 无日期数据给少量分

        rules = cfg.SCORE_RULES["data_recency_months"]
        # 按月数区间匹配
        for threshold, points in sorted(rules.items()):
            if months <= threshold:
                return points
        return 0

    def calculate_rule_score(self, lead: dict) -> tuple[int, dict]:
        """
        计算规则总分（0-100）。
        返回 (总分, 各维度明细)
        """
        breakdown = {
            "import_frequency": self.score_import_frequency(lead.get("import_count_6m")),
            "product_match":    self.score_product_match(lead.get("hs_codes")),
            "market_priority":  self.score_market_priority(lead.get("country")),
            "company_verify":   self.score_company_verify(lead),
            "data_recency":     self.score_data_recency(lead.get("last_import_date")),
        }
        total = sum(breakdown.values())
        return total, breakdown

    # ─────────────────────────────────────────
    # 阶段 2：Claude AI 深度分析
    # ─────────────────────────────────────────

    def build_prompt(self, lead: dict, rule_score: int, breakdown: dict) -> str:
        """构建发给Claude的评估Prompt"""
        return f"""你是一个B2B外贸客户评估专家，专注于摩托车发动机出口（中国重庆工厂→全球）。

请分析以下潜在采购商信息，在规则评分基础上给出专业调整：

═══ 客户信息 ═══
公司名称：{lead.get('company_name', '未知')}
国家/地区：{lead.get('country', '未知')}（{lead.get('region', '')}市场）
官网：{lead.get('website') or '无'}
联系人职位：{lead.get('contact_title') or '未知'}

═══ 采购数据 ═══
HS编码：{lead.get('hs_codes') or '无记录'}
近6个月进口次数：{lead.get('import_count_6m') if lead.get('import_count_6m') is not None else '未知'}
最近进口时间：{lead.get('last_import_date') or '未知'}
估计进口金额：{f"${lead.get('estimated_value_usd'):,.0f}" if lead.get('estimated_value_usd') else '未知'}

═══ 规则评分：{rule_score}/100 ═══
  进口频率：{breakdown.get('import_frequency')}/30
  产品匹配：{breakdown.get('product_match')}/25
  市场优先：{breakdown.get('market_priority')}/20
  公司验证：{breakdown.get('company_verify')}/15
  数据新鲜：{breakdown.get('data_recency')}/10

═══ 评估要求 ═══
请基于你对摩托车发动机B2B市场的了解，判断：
1. 这家公司是否真的在采购摩托车发动机（而非其他产品）？
2. 联系人职位是否有采购决策权？
3. 该市场对中国摩托车发动机的需求特点？
4. 建议用什么策略联系效果最好？

请只返回以下JSON格式，不要其他内容：
{{
  "adjustment": <整数，范围-10到+10，在规则分基础上调整>,
  "reasoning": "<评估理由，100字以内，中文>",
  "approach": "<建议联系策略，50字以内，中文>",
  "flags": ["<风险点1>", "<风险点2>"]
}}"""

    def ai_analyze(self, lead: dict, rule_score: int,
                   breakdown: dict) -> dict:
        """
        使用统一 AI 路由层做深度分析。
        规则分 >= 75 → Claude 把关（精度优先，A级客户）
        规则分 50-74 → DeepSeek（成本优先，B级候选）
        """
        from ai_provider import ai as ai_router
        if not ai_router.available():
            logger.warning("未配置任何 AI（DeepSeek/Claude），跳过AI分析")
            return {"adjustment": 0, "reasoning": "（未配置AI）",
                    "approach": "按标准流程联系", "flags": []}

        result = ai_router.deep_score_lead(lead, rule_score)
        logger.debug(
            f"AI分析完成: {lead.get('company_name')} "
            f"调整 {result.get('adjustment',0):+d}分 "
            f"(模型:{result.get('model_used','unknown')})"
        )
        return result

    # ─────────────────────────────────────────
    # 对单条 lead 完整评分
    # ─────────────────────────────────────────

    def score_one(self, lead: dict, use_ai: bool = True) -> dict:
        """
        对单条 lead 完成全部评分，返回评分结果字典。
        不写数据库，由调用方决定是否保存。
        """
        lead_id = lead["id"]
        company = lead.get("company_name", "未知")

        # 阶段1: 规则打分
        rule_score, breakdown = self.calculate_rule_score(lead)
        logger.debug(f"规则评分: {company} = {rule_score}分 {breakdown}")

        # 阶段2: AI深度分析（只对达到阈值的leads做，省token）
        ai_result = {"adjustment": 0, "reasoning": "", "approach": "", "flags": []}
        if use_ai and rule_score >= cfg.AI_SCORE_MIN_THRESHOLD and self.client:
            ai_result = self.ai_analyze(lead, rule_score, breakdown)
            # 限速：AI分析之间间隔随机1-3秒
            time.sleep(random.uniform(1, 3))

        final_score = rule_score + ai_result["adjustment"]
        final_score = max(0, min(100, final_score))  # 钳在0-100

        grade = self._calc_grade(final_score)

        return {
            "lead_id": lead_id,
            "rule_score": rule_score,
            "ai_score_adjustment": ai_result["adjustment"],
            "final_score": final_score,
            "grade": grade,
            "ai_reasoning": ai_result["reasoning"],
            "recommended_approach": ai_result["approach"],
            "risk_flags": ai_result["flags"],
            "score_breakdown": breakdown,   # 仅供打印，不存库
        }

    @staticmethod
    def _calc_grade(score: int) -> str:
        thresholds = cfg.GRADE_THRESHOLDS
        if score >= thresholds["A"]:
            return "A"
        elif score >= thresholds["B"]:
            return "B"
        elif score >= thresholds["C"]:
            return "C"
        return "D"

    # ─────────────────────────────────────────
    # 批量评分主流水线
    # ─────────────────────────────────────────

    def run(self, lead_ids: list[str] = None, use_ai: bool = True) -> dict:
        """
        批量评分流水线。

        lead_ids: 指定要评分的ID列表，None = 处理所有 status='new' 的leads
        use_ai:   是否启用Claude AI分析（可在无API key时关闭）

        返回统计：
        {
            "total": 处理总数,
            "scored": 成功评分数,
            "grade_A": A级数量,
            "grade_B": B级数量,
            "grade_C": C级数量,
            "grade_D": D级数量,
            "ai_analyzed": AI深度分析数量,
            "errors": 失败数,
        }
        """
        _db = self._resolve_db()

        # 获取待评分leads
        if lead_ids:
            leads = [_db.get_lead(lid) for lid in lead_ids]
            leads = [l for l in leads if l]  # 过滤None
        else:
            leads = _db.get_leads_for_scoring()

        if not leads:
            logger.info("没有待评分的leads")
            return {"total": 0, "scored": 0}

        ai_available = use_ai and bool(self.client)
        logger.info(
            f"开始评分: {len(leads)} 条leads，"
            f"AI分析: {'开启' if ai_available else '关闭（无API Key）'}"
        )
        if ai_available:
            ai_candidates = [l for l in leads
                             if (l.get("import_count_6m") or 0) >= 0]  # 预估AI调用量
            logger.info(
                f"规则分 >= {cfg.AI_SCORE_MIN_THRESHOLD} 的leads将做AI深度分析"
            )

        stats = {
            "total": len(leads),
            "scored": 0,
            "grade_A": 0, "grade_B": 0, "grade_C": 0, "grade_D": 0,
            "ai_analyzed": 0,
            "errors": 0,
        }

        for i, lead in enumerate(leads, 1):
            company = lead.get("company_name", "未知")
            try:
                result = self.score_one(lead, use_ai=use_ai)

                # 写回数据库
                _db.update_lead_score(
                    lead_id=result["lead_id"],
                    rule_score=result["rule_score"],
                    ai_adjustment=result["ai_score_adjustment"],
                    ai_reasoning=result["ai_reasoning"],
                    recommended_approach=result["recommended_approach"],
                    risk_flags=result["risk_flags"],
                )

                stats["scored"] += 1
                grade_key = f"grade_{result['grade']}"
                stats[grade_key] = stats.get(grade_key, 0) + 1
                if result["ai_score_adjustment"] != 0:
                    stats["ai_analyzed"] += 1

                logger.info(
                    f"[{i}/{len(leads)}] {company}: "
                    f"规则{result['rule_score']} + AI{result['ai_score_adjustment']:+d} "
                    f"= {result['final_score']}分 ({result['grade']}级)"
                )

            except Exception as e:
                stats["errors"] += 1
                logger.error(f"评分失败: {company} — {e}")

        logger.success(
            f"评分完成: "
            f"A={stats['grade_A']} B={stats['grade_B']} "
            f"C={stats['grade_C']} D={stats['grade_D']} "
            f"AI分析={stats['ai_analyzed']} 失败={stats['errors']}"
        )
        return stats

    def print_score_report(self, lead: dict) -> None:
        """打印单条lead的详细评分报告（调试用）"""
        result = self.score_one(lead, use_ai=False)
        bd = result["score_breakdown"]
        print(f"""
┌─ 评分报告 ─────────────────────────────────
│ 公司: {lead.get('company_name')}
│ 国家: {lead.get('country')} | 区域: {lead.get('region')}
├─ 各维度得分 ────────────────────────────────
│ 进口频率: {bd['import_frequency']:>3}/30  （近6月进口 {lead.get('import_count_6m') or '未知'} 次）
│ 产品匹配: {bd['product_match']:>3}/25  （HS: {lead.get('hs_codes')}）
│ 市场优先: {bd['market_priority']:>3}/20  （{cfg.get_country_tier(lead.get('country',''))}）
│ 公司验证: {bd['company_verify']:>3}/15  （官网:{bool(lead.get('website'))} LinkedIn:{bool(lead.get('linkedin_url'))}）
│ 数据新鲜: {bd['data_recency']:>3}/10  （最近进口: {lead.get('last_import_date') or '未知'}）
├─ 总分 ──────────────────────────────────────
│ 规则分: {result['rule_score']}/100  →  等级: {result['grade']}
└────────────────────────────────────────────""")


# 单例
scorer = Scorer()

# app.py 里用 LeadScorer(db_path=...) 导入，保持兼容（同 module2_cleaner 的 DataCleaner=Cleaner）
LeadScorer = Scorer


# ─────────────────────────────────────────
# 直接运行此文件 = 评分测试
# ─────────────────────────────────────────
if __name__ == "__main__":
    import sys
    import logging
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

    db.init()

    print("=" * 55)
    print("测试 1: 各维度单独打分")
    print(f"  进口频率 0次: {scorer.score_import_frequency(0)}/30")
    print(f"  进口频率 3次: {scorer.score_import_frequency(3)}/30")
    print(f"  进口频率 7次: {scorer.score_import_frequency(7)}/30")
    print(f"  进口频率12次: {scorer.score_import_frequency(12)}/30")
    print(f"  HS=8407 (发动机): {scorer.score_product_match(['8407'])}/25")
    print(f"  HS=8714 (配件):   {scorer.score_product_match(['8714'])}/25")
    print(f"  HS=未知:          {scorer.score_product_match([])}/25")
    print(f"  Nigeria(tier1):   {scorer.score_market_priority('Nigeria')}/20")
    print(f"  India(tier2):     {scorer.score_market_priority('India')}/20")
    print(f"  Europe(other):    {scorer.score_market_priority('Germany')}/20")

    print("\n测试 2: 完整规则评分")
    mock_leads = [
        {
            "id": "test-A",
            "company_name": "Lagos Engine Traders",
            "country": "Nigeria",
            "region": "Africa",
            "website": "https://lagosengine.ng",
            "email": "buy@lagosengine.ng",
            "linkedin_url": "https://linkedin.com/company/lagosengine",
            "contact_name": "Emeka Obi",
            "contact_title": "Head of Procurement",
            "hs_codes": ["8407"],
            "import_count_6m": 11,
            "last_import_date": "2024-11",
            "estimated_value_usd": 85000,
        },
        {
            "id": "test-B",
            "company_name": "Saigon Moto Parts",
            "country": "Vietnam",
            "region": "SEA",
            "website": "https://saigonmoto.vn",
            "email": "info@saigonmoto.vn",
            "hs_codes": ["8714"],
            "import_count_6m": 5,
            "last_import_date": "2024-09",
        },
        {
            "id": "test-D",
            "company_name": "Unknown Trading Co",
            "country": "Germany",
            "region": "Other",
            "hs_codes": [],
            "import_count_6m": 0,
        },
    ]

    for lead in mock_leads:
        scorer.print_score_report(lead)

    print("\n测试 3: 批量评分（不含AI，因为无API Key）")
    # 先把mock数据写入数据库
    from module2_cleaner import cleaner
    db_ready = []
    for l in mock_leads:
        # 重新用cleaner标准化
        l_copy = l.copy()
        l_copy.setdefault("sources", ["test"])
        l_copy["company_name_norm"] = cleaner.normalize_company_name(l["company_name"])
        db.insert_lead(l_copy)
        db_ready.append(l_copy)

    stats = scorer.run(use_ai=False)   # 不用AI，直接规则打分
    print(f"\n  评分统计: {stats}")
    assert stats["grade_A"] >= 1   # Lagos那条应该是A
    assert stats["grade_D"] >= 1   # Unknown那条应该是D
    print("  ✅ 批量评分测试通过")

    # 验证数据库已更新
    leads_a, _ = db.search_leads(grade="A")
    print(f"\n  A级客户: {[l['company_name'] for l in leads_a]}")
    leads_d, _ = db.search_leads(grade="D")
    print(f"  D级客户: {[l['company_name'] for l in leads_d]}")

    print("\n✅ 评分模块所有测试通过")
    print("=" * 55)
