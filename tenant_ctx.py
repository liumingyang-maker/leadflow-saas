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


REGIONS = {
    "非洲": [
        "Nigeria", "Ethiopia", "Egypt", "DR Congo", "Tanzania", "Kenya",
        "South Africa", "Uganda", "Algeria", "Sudan", "Morocco", "Angola",
        "Mozambique", "Ghana", "Ivory Coast", "Cameroon", "Niger", "Burkina Faso",
        "Mali", "Malawi", "Zambia", "Senegal", "Zimbabwe", "Chad", "Guinea",
        "Rwanda", "Benin", "Burundi", "Tunisia", "Libya", "Togo",
        "Sierra Leone", "Eritrea", "Central African Republic", "Mauritania",
        "Botswana", "Namibia", "Gambia", "Gabon",
    ],
    "东南亚": [
        "Indonesia", "Philippines", "Vietnam", "Thailand", "Myanmar",
        "Malaysia", "Cambodia", "Laos", "Singapore", "Brunei", "East Timor",
    ],
    "南亚": [
        "India", "Pakistan", "Bangladesh", "Nepal", "Sri Lanka",
        "Afghanistan", "Maldives",
    ],
    "中东": [
        "Turkey", "Saudi Arabia", "UAE", "Iraq", "Iran", "Yemen",
        "Syria", "Jordan", "Kuwait", "Oman", "Qatar", "Bahrain",
        "Lebanon", "Israel", "Palestine",
    ],
    "北美": [
        "USA", "Canada", "Mexico",
    ],
    "拉丁美洲": [
        "Brazil", "Colombia", "Argentina", "Peru", "Venezuela", "Chile",
        "Ecuador", "Bolivia", "Paraguay", "Uruguay", "Guatemala", "Honduras",
        "El Salvador", "Nicaragua", "Costa Rica", "Panama",
        "Dominican Republic", "Cuba", "Haiti", "Jamaica", "Trinidad and Tobago",
    ],
    "西欧": [
        "Germany", "UK", "France", "Italy", "Spain", "Netherlands",
        "Belgium", "Sweden", "Norway", "Denmark", "Finland", "Switzerland",
        "Austria", "Portugal", "Greece", "Ireland", "Czech Republic",
        "Hungary", "Romania", "Poland", "Bulgaria", "Croatia", "Slovakia",
        "Slovenia", "Serbia", "Bosnia", "Albania", "North Macedonia",
    ],
    "东欧/中亚": [
        "Russia", "Ukraine", "Kazakhstan", "Belarus", "Uzbekistan",
        "Azerbaijan", "Georgia", "Armenia", "Kyrgyzstan", "Tajikistan",
        "Turkmenistan", "Moldova",
    ],
    "大洋洲": [
        "Australia", "New Zealand", "Papua New Guinea", "Fiji",
    ],
}

ALL_COUNTRIES = [c for countries in REGIONS.values() for c in countries]

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
