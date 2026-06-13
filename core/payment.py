"""
core/payment.py — 自助支付（可插拔多渠道）
=============================================
第一个适配器：虎皮椒（xunhupay）个人聚合支付——买家扫微信/支付宝付款，
钱结算到内地（平台方的微信/支付宝/内地银行卡）。商户凭据是**平台级**的，
放在环境变量（config.XUNHUPAY_APPID / XUNHUPAY_SECRET），不是每租户。

安全要点：
  · 金额一律由服务端 PLAN_PRICES 决定，绝不信前端传来的金额。
  · 只在「服务端异步回调 /pay/notify 验签通过」后才升级账号，不靠前端跳转。
  · 回调验签：虎皮椒用 MD5(排序后的 k=v& 串 + APPSECRET)。
  · 回调要幂等（可能重发），由 admin_db.mark_order_paid 保证只入账一次。

加新渠道（Stripe/官方微信）只要再写一个 create_order/verify_notify 适配器即可。
"""
import time
import random
import string
import hashlib

import requests

try:
    import config
except Exception:                                  # pragma: no cover
    config = None


def is_configured() -> bool:
    return bool(getattr(config, "XUNHUPAY_APPID", "") and
                getattr(config, "XUNHUPAY_SECRET", ""))


def _nonce(n: int = 16) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=n))


def _xunhupay_sign(params: dict, secret: str) -> str:
    """虎皮椒签名：剔除 hash/空值 → 按 key 升序拼 k=v& → 去尾 & → 末尾接 APPSECRET → MD5。"""
    items = {k: v for k, v in params.items()
             if k != "hash" and v not in ("", None)}
    s = "&".join(f"{k}={items[k]}" for k in sorted(items))
    return hashlib.md5((s + secret).encode("utf-8")).hexdigest()


def create_order(order_id: str, amount_cny, title: str,
                 notify_url: str, return_url: str) -> dict:
    """虎皮椒下单。返回 {ok, qrcode(二维码图URL), url(收银台URL), raw} 或 {ok:False,error}。"""
    if not is_configured():
        return {"ok": False, "error": "平台尚未配置支付（联系客服开通）"}
    appid  = config.XUNHUPAY_APPID
    secret = config.XUNHUPAY_SECRET
    gateway = getattr(config, "XUNHUPAY_GATEWAY",
                      "https://api.xunhupay.com/payment/do.html")
    params = {
        "version":        "1.1",
        "appid":          appid,
        "trade_order_id": order_id,
        "total_fee":      f"{amount_cny}",
        "title":          title,
        "time":           str(int(time.time())),
        "notify_url":     notify_url,
        "return_url":     return_url,
        "nonce_str":      _nonce(),
    }
    params["hash"] = _xunhupay_sign(params, secret)
    try:
        r = requests.post(gateway, data=params, timeout=20)
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": f"下单请求失败：{e}"}
    if str(data.get("errcode")) == "0":
        return {"ok": True,
                "qrcode": data.get("url_qrcode") or "",
                "url":    data.get("url") or "",
                "raw":    data}
    return {"ok": False, "error": data.get("errmsg", "下单失败"), "raw": data}


def verify_notify(params: dict) -> bool:
    """校验虎皮椒异步回调签名。"""
    secret = getattr(config, "XUNHUPAY_SECRET", "")
    if not secret:
        return False
    recv = params.get("hash", "")
    return bool(recv) and recv == _xunhupay_sign(params, secret)


def notify_is_paid(params: dict) -> bool:
    """虎皮椒回调里 status=='OD' 表示支付成功。"""
    return str(params.get("status", "")).upper() == "OD"
