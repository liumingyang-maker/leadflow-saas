"""
tenant_ctx.py — 租户上下文
每个请求通过 Flask session 知道当前是哪个租户，
提供该租户的配置和数据库路径。
"""
import json
import sys
import os
from pathlib import Path

BASE = Path(__file__).parent

# 核心模块路径（core/ 目录）
CORE_PATH = Path(__file__).parent / "core"
if str(CORE_PATH) not in sys.path:
    sys.path.insert(0, str(CORE_PATH))


def tenant_dir(tid: str) -> Path:
    return BASE / "tenants" / tid


def load_config(tid: str) -> dict:
    cfg_path = tenant_dir(tid) / "config.json"
    if cfg_path.exists():
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    return {}


def save_config(tid: str, cfg: dict):
    cfg_path = tenant_dir(tid) / "config.json"
    cfg_path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_db_path(tid: str) -> str:
    return str(tenant_dir(tid) / "leads.db")


def get_email_templates_path(tid: str) -> Path:
    return tenant_dir(tid) / "email_templates.json"


def get_collection_rules_path(tid: str) -> Path:
    return tenant_dir(tid) / "collection_rules.json"


# 默认可选国家列表（行业通用）
ALL_COUNTRIES = [
    # 非洲
    "Nigeria", "Tanzania", "Kenya", "Ghana", "Uganda", "Ethiopia",
    "Mozambique", "Senegal", "Cameroon", "Ivory Coast", "South Africa",
    "Egypt", "Morocco", "Algeria",
    # 东南亚
    "Vietnam", "Indonesia", "Thailand", "Philippines", "Myanmar",
    "Malaysia", "Cambodia",
    # 南亚
    "Pakistan", "Bangladesh", "Nepal", "Sri Lanka",
    # 拉丁美洲
    "Brazil", "Mexico", "Colombia", "Peru", "Argentina", "Chile",
    "Ecuador", "Bolivia",
    # 中东
    "UAE", "Saudi Arabia", "Iran", "Iraq",
    # 东欧/中亚
    "Russia", "Kazakhstan", "Ukraine",
]

REGIONS = {
    "非洲":   ["Nigeria","Tanzania","Kenya","Ghana","Uganda","Ethiopia",
               "Mozambique","Senegal","Cameroon","Ivory Coast","South Africa",
               "Egypt","Morocco","Algeria"],
    "东南亚": ["Vietnam","Indonesia","Thailand","Philippines","Myanmar",
               "Malaysia","Cambodia"],
    "南亚":   ["Pakistan","Bangladesh","Nepal","Sri Lanka"],
    "拉丁美洲":["Brazil","Mexico","Colombia","Peru","Argentina","Chile",
               "Ecuador","Bolivia"],
    "中东":   ["UAE","Saudi Arabia","Iran","Iraq"],
    "东欧/中亚":["Russia","Kazakhstan","Ukraine"],
}

INDUSTRY_OPTIONS = [
    ("motorcycle", "摩托车/摩配"),
    ("auto_parts", "汽车配件"),
    ("machinery",  "机械设备"),
    ("electronics","电子电器"),
    ("textile",    "纺织服装"),
    ("hardware",   "五金工具"),
    ("agriculture","农业机械"),
    ("chemical",   "化工原料"),
    ("furniture",  "家具建材"),
    ("other",      "其他行业"),
]
