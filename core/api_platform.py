"""
core/api_platform.py — 平台代付 API key + 会员额度
====================================================
模式（与商业方案一致）：
  · DeepSeek + Serper 由平台垫钱（主 key 在服务器环境变量 DEEPSEEK_MASTER_KEY /
    SERPER_MASTER_KEYS）。客户**零配置即用**——入驻向导不再让填 key。
  · 按会员档位限额（free/trial/pro/ultra），调用前卡额度、按月自动重置
    （admin_db.api_usage 的 period=YYYY-MM）。
  · 客户在设置里填了自己的 key（BYOK）→ 用他的、**不限额、不计入平台用量**。
    贵的海关 ImportYeti / Apollo / Apify 仍只走 BYOK（留作 Ultra 未来项目）。

安全要点：
  · runtime_cfg() 返回的是 raw config 的 **副本** + 注入平台 key，**绝不回存**，
    避免把平台主 key 写进客户的 config.json。
  · 设置页 / 额度查询用 raw config（tenant_ctx.load_config），不走注入。
  · 主 key 未配置（env 为空）时 runtime_cfg 不注入任何东西 = 行为同今天，可安全先部署。
"""
import config
import tenant_ctx
import admin_db

try:
    from api_quota import parse_keys
except Exception:                                  # pragma: no cover
    from core.api_quota import parse_keys


# 各会员档位每月额度（平台代付时生效；BYOK 不受此限）。
# serper —— 已 enforce（serper_keys_for 调用前卡额度）。
# deepseek —— 暂【仅作政策记录，尚未 enforce】（按方案 B，DeepSeek 便宜+大头被 Serper 闸间接限，
#   以后做"中央 DeepSeek 计数器"时直接读这里的数字硬卡。单位=AI 调用次/月）。
PLAN_QUOTA = {
    "free":      {"serper": 100,   "deepseek": 300},
    "trial":     {"serper": 300,   "deepseek": 800},
    "pro":       {"serper": 2500,  "deepseek": 6000},
    "ultra":     {"serper": 10000, "deepseek": 25000},
    "suspended": {"serper": 0,     "deepseek": 0},
}


def tenant_tier(tid: str) -> str:
    """租户有效档位：plan 明确是 free/pro/ultra 就用它；否则按 status 推
    （suspended→停用；active→pro；其余→trial）。早期付款手动改 plan/status 即可。"""
    try:
        t = admin_db.get_tenant(tid) or {}
    except Exception:
        t = {}
    plan = (t.get("plan") or "").lower()
    if plan in ("pro", "ultra"):
        # 付费档位看到期日：过期 → 自动降级为 free
        exp = t.get("plan_expires_at")
        if exp:
            try:
                from datetime import datetime, timezone
                exp_dt = datetime.strptime(exp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if exp_dt < datetime.now(timezone.utc):
                    return "free"
            except Exception:
                pass
        return plan
    if plan == "free":
        return "free"
    status = (t.get("status") or "").lower()
    if status == "suspended":
        return "suspended"
    if status == "active":
        return "pro"
    return "trial"


def serper_quota(tid: str) -> int:
    return PLAN_QUOTA.get(tenant_tier(tid), PLAN_QUOTA["free"]).get("serper", 0)


def runtime_cfg(tid: str) -> dict:
    """运行用配置 = raw config 副本 + 注入平台主 key（仅当客户没填自己的）。
    打 _serper_platform / _deepseek_platform 标记供计量识别。**绝不回存。**"""
    cfg = dict(tenant_ctx.load_config(tid) or {})
    if not parse_keys(cfg.get("serpapi_key")):
        masters = parse_keys(getattr(config, "SERPER_MASTER_KEYS", ""))
        if masters:
            cfg["serpapi_key"] = ",".join(masters)
            cfg["_serper_platform"] = True
    if not (cfg.get("deepseek_api_key") or "").strip():
        if getattr(config, "DEEPSEEK_MASTER_KEY", ""):
            cfg["deepseek_api_key"] = config.DEEPSEEK_MASTER_KEY
            cfg["_deepseek_platform"] = True
    return cfg


def serper_keys_for(tid: str, cfg: dict):
    """采集器要用的 Serper key 列表 + 是否平台代付 + 是否已超额 + 原因。
    返回 (keys, is_platform, blocked, reason)。blocked 时 keys=[]，采集器据此优雅跳过 Serper 源。"""
    keys = parse_keys(cfg.get("serpapi_key"))
    is_platform = bool(cfg.get("_serper_platform"))
    if not keys:
        return [], is_platform, False, None
    if not is_platform:
        return keys, False, False, None        # BYOK：不限额
    used = admin_db.get_api_usage(tid, "serper")
    limit = serper_quota(tid)
    if limit and used >= limit:
        return [], True, True, (
            f"本月平台 Serper 额度已用完（{used}/{limit}，{tenant_tier(tid)} 档）。"
            "升级会员、下月 1 号自动重置，或在「系统设置」填自己的 Serper Key 继续。")
    return keys, True, False, None


def record_serper(tid: str, cfg: dict, calls: int) -> None:
    """只统计平台代付的消耗（BYOK 不计入平台额度）。"""
    if calls and calls > 0 and cfg.get("_serper_platform"):
        admin_db.add_api_usage(tid, "serper", calls)


def quota_status(tid: str, cfg_raw: dict) -> dict:
    """工作台「API 额度」卡用：区分平台代付 / BYOK。传 raw config（不是 runtime）。"""
    out = {}
    own_serper = parse_keys(cfg_raw.get("serpapi_key"))
    if own_serper:
        out["serper"] = {"source": "byok", "keys": len(own_serper)}
    else:
        used = admin_db.get_api_usage(tid, "serper")
        limit = serper_quota(tid)
        out["serper"] = {"source": "platform", "tier": tenant_tier(tid),
                         "used": used, "limit": limit,
                         "remaining": max(0, limit - used)}
    out["deepseek"] = {"source": "byok" if (cfg_raw.get("deepseek_api_key") or "").strip()
                       else "platform"}
    return out
