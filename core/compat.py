"""
core/compat.py — 兼容层
让从旧 leads 项目复制过来的模块在 SaaS 环境中正常运行，
提供 cfg 和 logger 的安全替代品。
"""
import logging
from pathlib import Path

# ── logger ──────────────────────────────────────────────
try:
    from log_setup import logger
except ImportError:
    logger = logging.getLogger("leadflow")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        logger.addHandler(logging.StreamHandler())


# ── cfg 占位符 ───────────────────────────────────────────
class _SafeCfg:
    IMPORTYETI_API_KEY  = ""
    HUNTER_API_KEY      = ""
    PROXYCURL_API_KEY   = ""
    CLAUDE_API_KEY      = ""
    ANTHROPIC_API_KEY   = ""
    TARGET_COUNTRIES    = []
    CACHE_DIR           = Path(__file__).parent / "cache"
    REQUEST_DELAY_MIN   = 1.0
    REQUEST_DELAY_MAX   = 3.0
    AI_SCORE_MIN_THRESHOLD = 60
    DB_PATH             = "leads.db"
    GRADE_THRESHOLDS    = {"A": 80, "B": 60, "C": 40}
    SCORE_RULES = {
        "import_frequency": {
            "very_high": 20, "high": 15, "medium": 10, "low": 5, "none": 0
        },
        "product_match": {
            "exact": 25, "related": 15, "unclear": 5
        },
        "data_recency_months": {6: 20, 12: 15, 24: 10, 999: 5},
    }

    def get_country_tier(self, country: str) -> str:
        return "tier3"

    def get_hs_codes_for_country(self, country: str) -> list:
        return []


try:
    from config import cfg  # 旧项目 config 存在时使用
    if not hasattr(cfg, "GRADE_THRESHOLDS"):
        raise ImportError
except Exception:
    cfg = _SafeCfg()
