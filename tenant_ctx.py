"""
tenant_ctx.py — 租户上下文
每个请求通过 Flask session 知道当前是哪个租户，
提供该租户的配置和数据库路径。
"""
import json
import sys
import os
import base64
import hashlib
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken

BASE = Path(__file__).parent

# 数据目录：默认项目根目录；部署挂载持久卷时用环境变量 DATA_DIR 指向卷（如 /data）
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE)))

SECRET_FIELDS = {
    "importyeti_api_key",
    "serpapi_key",
    "hunter_api_key",
    "deepseek_api_key",
    "anthropic_api_key",
    "apollo_api_key",
    "youtube_api_key",
    "apify_token",
    "radar_proxy",
    "smtp_pass",
    "esp_api_key",
    "wa_auth_token",
    "wa_api_key",
    "wa_token",
}
SECRET_PREFIX = "enc:v1:"


class SecretStoreError(RuntimeError):
    pass


def _secret_key_material() -> str:
    key = os.environ.get("TENANT_SECRET_KEY") or os.environ.get("SECRET_KEY")
    if key:
        return key
    env = (
        os.environ.get("APP_ENV")
        or os.environ.get("FLASK_ENV")
        or os.environ.get("ENV")
        or ""
    ).strip().lower()
    key_file = DATA_DIR / "secret_key.bin"
    if env not in {"prod", "production"} and key_file.exists():
        return key_file.read_bytes().hex()
    raise SecretStoreError("TENANT_SECRET_KEY or SECRET_KEY is required for tenant secrets")


def _fernet_for(key: str) -> Fernet:
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _fernet() -> Fernet:
    return _fernet_for(_secret_key_material())


def _decrypt_fernets() -> list[Fernet]:
    fernets = [_fernet()]
    previous = os.environ.get("TENANT_SECRET_KEY_PREVIOUS", "").strip()
    if previous:
        fernets.append(_fernet_for(previous))
    return fernets


def _is_encrypted_secret(value) -> bool:
    return isinstance(value, str) and value.startswith(SECRET_PREFIX)


def _encrypt_secret(value: str) -> str:
    token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return SECRET_PREFIX + token


def _decrypt_secret(field: str, value: str) -> str:
    token = value[len(SECRET_PREFIX):].encode("ascii")
    last_error = None
    for fernet in _decrypt_fernets():
        try:
            return fernet.decrypt(token).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
            last_error = exc
    raise SecretStoreError(f"Unable to decrypt tenant secret: {field}") from last_error


def encrypt_secret_value(value: str) -> str:
    return _encrypt_secret(value)


def decrypt_secret_value(field: str, value: str) -> str:
    return _decrypt_secret(field, value)


def _decrypt_config(cfg: dict) -> dict:
    out = dict(cfg)
    for field in SECRET_FIELDS:
        value = out.get(field)
        if _is_encrypted_secret(value):
            out[field] = _decrypt_secret(field, value)
    return out


def _encrypt_config(cfg: dict) -> dict:
    out = dict(cfg)
    for field in SECRET_FIELDS:
        value = out.get(field)
        if isinstance(value, str) and value and not _is_encrypted_secret(value):
            out[field] = _encrypt_secret(value)
    return out


def secret_label(value: str) -> str:
    if not value:
        return ""
    return f"saved: ****{value[-4:]}"


def mask_config_secrets(cfg: dict) -> tuple[dict, dict]:
    masked = dict(cfg)
    labels = {}
    for field in SECRET_FIELDS:
        value = masked.get(field)
        if isinstance(value, str) and value:
            labels[field] = secret_label(value)
            masked[field] = ""
    return masked, labels

# 核心模块路径（core/ 目录）
CORE_PATH = Path(__file__).parent / "core"
if str(CORE_PATH) not in sys.path:
    sys.path.insert(0, str(CORE_PATH))


def tenant_dir(tid: str) -> Path:
    return DATA_DIR / "tenants" / tid


def load_config(tid: str) -> dict:
    cfg_path = tenant_dir(tid) / "config.json"
    if cfg_path.exists():
        return _decrypt_config(json.loads(cfg_path.read_text(encoding="utf-8")))
    return {}


def save_config(tid: str, cfg: dict):
    cfg_path = tenant_dir(tid) / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(_encrypt_config(cfg), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_db_path(tid: str) -> str:
    return str(tenant_dir(tid) / "leads.db")


def get_email_templates_path(tid: str) -> Path:
    return tenant_dir(tid) / "email_templates.json"


def get_collection_rules_path(tid: str) -> Path:
    return tenant_dir(tid) / "collection_rules.json"


REGIONS = {
    "非洲": [
        "尼日利亚", "埃塞俄比亚", "埃及", "刚果（金）", "坦桑尼亚", "肯尼亚",
        "南非", "乌干达", "阿尔及利亚", "苏丹", "摩洛哥", "安哥拉",
        "莫桑比克", "加纳", "科特迪瓦", "喀麦隆", "尼日尔", "布基纳法索",
        "马里", "马拉维", "赞比亚", "塞内加尔", "津巴布韦", "乍得",
        "几内亚", "卢旺达", "贝宁", "布隆迪", "突尼斯", "利比亚",
        "多哥", "塞拉利昂", "厄立特里亚", "中非共和国", "毛里塔尼亚",
        "博茨瓦纳", "纳米比亚", "冈比亚", "加蓬",
    ],
    "东南亚": [
        "印度尼西亚", "菲律宾", "越南", "泰国", "缅甸",
        "马来西亚", "柬埔寨", "老挝", "新加坡", "文莱", "东帝汶",
    ],
    "南亚": [
        "印度", "巴基斯坦", "孟加拉国", "尼泊尔", "斯里兰卡",
        "阿富汗", "马尔代夫",
    ],
    "中东": [
        "土耳其", "沙特阿拉伯", "阿联酋", "伊拉克", "伊朗", "也门",
        "叙利亚", "约旦", "科威特", "阿曼", "卡塔尔", "巴林",
        "黎巴嫩", "以色列", "巴勒斯坦",
    ],
    "北美": [
        "美国", "加拿大", "墨西哥",
    ],
    "拉丁美洲": [
        "巴西", "哥伦比亚", "阿根廷", "秘鲁", "委内瑞拉", "智利",
        "厄瓜多尔", "玻利维亚", "巴拉圭", "乌拉圭", "危地马拉", "洪都拉斯",
        "萨尔瓦多", "尼加拉瓜", "哥斯达黎加", "巴拿马",
        "多米尼加共和国", "古巴", "海地", "牙买加", "特立尼达和多巴哥",
    ],
    "西欧": [
        "德国", "英国", "法国", "意大利", "西班牙", "荷兰",
        "比利时", "瑞典", "挪威", "丹麦", "芬兰", "瑞士",
        "奥地利", "葡萄牙", "希腊", "爱尔兰", "捷克",
        "匈牙利", "罗马尼亚", "波兰", "保加利亚", "克罗地亚",
        "斯洛伐克", "斯洛文尼亚", "塞尔维亚", "波黑", "阿尔巴尼亚", "北马其顿",
    ],
    "东欧/中亚": [
        "俄罗斯", "乌克兰", "哈萨克斯坦", "白俄罗斯", "乌兹别克斯坦",
        "阿塞拜疆", "格鲁吉亚", "亚美尼亚", "吉尔吉斯斯坦",
        "塔吉克斯坦", "土库曼斯坦", "摩尔多瓦",
    ],
    "大洋洲": [
        "澳大利亚", "新西兰", "巴布亚新几内亚", "斐济",
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
