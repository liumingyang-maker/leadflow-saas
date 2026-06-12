"""
core/api_quota.py — 各 API 服务的额度/余额查询
================================================
诚实分两类：
  · 有官方余额接口的（DeepSeek 账户余额￥、Hunter.io 用量）→ 查【真实剩余】。
  · 没有公开余额接口的（Serper）→ 由本系统【本地计数】（admin_db.api_usage）
    对比客户在设置里填的套餐上限。只能统计经过本系统的调用。
结果做内存缓存（默认 10 分钟），避免工作台每次刷新都打外部接口。

另：parse_keys() 把"多 key"字段（换行/逗号分隔）拆成 key 列表 —— 多账号/多 key
    自动容错的单一事实来源（雷达 Serper 轮换、额度面板都用它）。
"""
import re
import time
import requests

_CACHE = {}          # (provider, key 尾段) -> (ts, result)
_TTL = 600           # 缓存 10 分钟


def parse_keys(raw) -> list:
    """多 key 字段（换行/逗号/分号/空格分隔）→ 去重保序的 key 列表。"""
    if not raw:
        return []
    items = raw if isinstance(raw, list) else re.split(r"[\s,;]+", str(raw))
    out = []
    for k in items:
        k = (k or "").strip()
        if k and k not in out:
            out.append(k)
    return out


def _fp(key):
    return (key or "")[-6:]


def _cached(provider, key):
    hit = _CACHE.get((provider, _fp(key)))
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    return None


def _store(provider, key, result):
    _CACHE[(provider, _fp(key))] = (time.time(), result)
    return result


def deepseek_balance(key: str) -> dict:
    """DeepSeek 账户余额（真实）。GET /user/balance，Bearer 鉴权。"""
    if not key:
        return {"ok": False, "reason": "未配置"}
    c = _cached("deepseek", key)
    if c is not None:
        return c
    try:
        r = requests.get("https://api.deepseek.com/user/balance",
                         headers={"Authorization": f"Bearer {key}"}, timeout=10)
        if r.status_code in (401, 403):
            return _store("deepseek", key, {"ok": False, "reason": "key 无效"})
        data = r.json() or {}
        infos = data.get("balance_infos") or []
        bal = infos[0].get("total_balance") if infos else None
        cur = infos[0].get("currency") if infos else "CNY"
        return _store("deepseek", key, {
            "ok": True, "kind": "balance",
            "balance": bal, "currency": cur,
            "available": bool(data.get("is_available")),
        })
    except Exception:
        return {"ok": False, "reason": "查询失败"}


def hunter_account(key: str) -> dict:
    """Hunter.io 账户用量（真实）。GET /v2/account。"""
    if not key:
        return {"ok": False, "reason": "未配置"}
    c = _cached("hunter", key)
    if c is not None:
        return c
    try:
        r = requests.get("https://api.hunter.io/v2/account",
                         params={"api_key": key}, timeout=10)
        if r.status_code in (401, 403):
            return _store("hunter", key, {"ok": False, "reason": "key 无效"})
        d = (r.json() or {}).get("data", {}) or {}
        searches = (d.get("requests", {}) or {}).get("searches", {}) or {}
        used = searches.get("used")
        avail = searches.get("available")
        remaining = (avail - used) if (isinstance(avail, int)
                                       and isinstance(used, int)) else None
        return _store("hunter", key, {
            "ok": True, "kind": "quota",
            "used": used, "limit": avail, "remaining": remaining,
        })
    except Exception:
        return {"ok": False, "reason": "查询失败"}
