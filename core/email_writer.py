"""
core/email_writer.py — AI 写开发信（适度个性化）
==================================================
按线索信息 + 客户自己的产品画像，用 DeepSeek 生成一封个性化外贸开发信。

设计要点（用户拍板）：
  · **适度个性化**：开头点买家所在「国家/市场 + 所属生意类型」，让对方觉得"是写给我的"，
    但**绝不点名竞品品牌**、不暴露"我们调查过你"。跨市场通用、稳妥。
  · **按买家国家自动选语言**（法语区→法语、拉美→西语、中东→阿语…），可手动覆盖。
  · 输出 {subject, body}，正文纯文本（发信时再转 HTML）。

用法：
    from email_writer import generate_email
    res = generate_email(deepseek_key, lead, profile, sender, tone="professional",
                         length="medium", lang=None, proxy="")
    # res = {"ok": True, "subject": "...", "body": "...", "lang": "fr"}
"""
import json
import requests

try:
    from module1_collectors.competitor_radar import resolve_focus_country
except Exception:                                  # pragma: no cover
    try:
        from core.module1_collectors.competitor_radar import resolve_focus_country
    except Exception:
        resolve_focus_country = None

LANG_NAME = {
    "en": "English", "fr": "French (Français)", "es": "Spanish (Español)",
    "pt": "Portuguese (Português)", "ar": "Arabic (العربية)",
}
TONE_DESC = {
    "professional": "professional, warm and trustworthy",
    "concise": "short, direct and punchy",
}
LENGTH_DESC = {
    "medium": "about 120-160 words",
    "short": "about 60-90 words",
}


def detect_lang(country: str) -> str:
    """从买家国家推断写信语言（默认英语）。复用渠道雷达的国家→语言解析。"""
    if country and resolve_focus_country:
        try:
            info = resolve_focus_country(country)
            if info and info.get("hl") in LANG_NAME:
                return info["hl"]
        except Exception:
            pass
    return "en"


def _as_list(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


def generate_email(deepseek_key: str, lead: dict, profile: dict, sender: dict,
                   tone: str = "professional", length: str = "medium",
                   lang: str = None, proxy: str = "") -> dict:
    if not deepseek_key:
        return {"ok": False, "error": "请先在系统设置填 DeepSeek API Key（或用平台额度）"}
    lead = lead or {}
    profile = profile or {}
    sender = sender or {}

    country = lead.get("country") or ""
    lang = lang if lang in LANG_NAME else detect_lang(country)
    lang_name = LANG_NAME.get(lang, "English")

    # 我方产品信息（客户的产品搜索画像 / 设置）
    category = profile.get("category") or sender.get("product_name") or "our products"
    keywords = ", ".join(_as_list(profile.get("keywords_en"))[:6])
    buyer_types = ", ".join(_as_list(profile.get("buyer_types"))[:4]) or "distributor / wholesaler / importer"

    # 买家上下文（**不含任何竞品品牌**，只用线索本身的公司/国家/网站）
    buyer_company = lead.get("company_name") or "your company"
    buyer_site = lead.get("website") or ""
    contact_name = lead.get("contact_name") or ""

    sender_company = sender.get("company_name") or sender.get("sender_name") or "our company"
    sender_name = sender.get("sender_name") or sender.get("email_from_name") or ""

    prompt = f"""You are an experienced B2B export sales writer. Write ONE cold outreach email \
from a supplier to a potential overseas buyer. The supplier exports: {category}\
{(' (keywords: ' + keywords + ')') if keywords else ''}.

Buyer to write to:
- Company: {buyer_company}
- Country/Market: {country or 'unknown'}
- Likely business type: {buyer_types}
- Website: {buyer_site or 'n/a'}
- Contact person: {contact_name or 'unknown'}

Supplier (the sender):
- Company: {sender_company}
- Sender name: {sender_name or '(leave a placeholder [Your Name])'}

Hard rules:
1. Write the email in {lang_name}. Subject line also in {lang_name}.
2. Personalize the opening by referencing the buyer's COUNTRY/MARKET and their type of \
business (e.g. as a {buyer_types} in {country or 'their market'}). Make it feel written for them.
3. DO NOT mention any competitor brand, do NOT imply you researched or monitored them, \
do NOT use creepy "I noticed you sell X brand" lines. Keep it natural and respectful.
4. Tone: {TONE_DESC.get(tone, TONE_DESC['professional'])}. Length: {LENGTH_DESC.get(length, LENGTH_DESC['medium'])}.
5. Include ONE clear, low-friction call to action (ask for interest / offer catalog or quote).
6. Sound human, not like spam. No exaggerated claims, no ALL CAPS, no excessive punctuation.

Output ONLY a JSON object, no markdown, no explanation:
{{"subject": "...", "body": "..."}}
The body is plain text with real line breaks (use \\n), ready to send. Do not include an \
unsubscribe line (the system adds it)."""

    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {deepseek_key}",
                     "Content-Type": "application/json"},
            json={
                "model": "deepseek-v4-flash",
                "thinking": {"type": "disabled"},
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "max_tokens": 900,
            },
            timeout=45, proxies=proxies,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return {"ok": False, "error": f"AI 生成失败：{e}"}

    obj = _parse_json_object(content)
    if not obj or not obj.get("body"):
        return {"ok": False, "error": "AI 返回格式异常，请重试"}
    return {"ok": True, "lang": lang,
            "subject": str(obj.get("subject", "")).strip()[:300],
            "body": str(obj.get("body", "")).strip()[:6000]}


def _parse_json_object(text: str) -> dict:
    """从模型输出里抠出第一个 JSON 对象（容错 markdown 代码块）。"""
    if not text:
        return {}
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1:]
    try:
        return json.loads(t)
    except Exception:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j != -1 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            return {}
    return {}
