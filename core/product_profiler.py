"""
product_profiler.py — 产品搜索画像生成器（AI 起草 + 客户确认）
================================================================
SaaS 卖给各行业客户，搜索好不好用取决于客户有没有把自己的产品描述准。
本模块让客户用大白话描述产品（或贴自己的产品页网址），DeepSeek 自动生成一份
结构化「产品搜索画像」草稿，客户改一改即存为全系统唯一的搜索词库。

画像字段（存进租户 config 的 product_profile）：
    category       产品类目（中文一句话）
    keywords_en    核心英文/行业术语关键词（买家和经销商真正会搜的词，最重要）
    synonyms       英文同义词/别称
    models         主力型号/规格示例
    applications   典型应用/用途
    buyer_types    目标买家类型（distributor/wholesaler/importer/OEM factory/...）
    hs_suggested   可能的 HS 海关编码
    keywords_i18n  多语言核心词（按目标市场语言：fr/es/pt/ar），配合渠道雷达本地化搜索

落地：app.py 的 /product-profile/generate 调用 generate_profile()，前端把草稿填进
设置页表单，客户确认后随 save_profile 一起存。保存时把 keywords_en+synonyms+models
派生进 search_keywords，让所有采集器零改动立即受益。
"""

import re

try:
    from ai_extractor import AIExtractor              # 抓页 + DeepSeek
except Exception:                                      # pragma: no cover
    from module1_collectors.ai_extractor import AIExtractor

try:
    from module1_collectors.competitor_radar import _COUNTRY_LANG
except Exception:                                      # pragma: no cover
    try:
        from competitor_radar import _COUNTRY_LANG
    except Exception:
        _COUNTRY_LANG = {}

_LANG_NAME = {
    "fr": "法语 French", "es": "西班牙语 Spanish",
    "pt": "葡萄牙语 Portuguese", "ar": "阿拉伯语 Arabic",
}


def target_langs(target_countries) -> list:
    """从目标国家推出需要翻译的语言（fr/es/pt/ar 中实际涉及的）。"""
    langs = []
    for c in target_countries or []:
        info = _COUNTRY_LANG.get(c)
        if info and info[1] in _LANG_NAME and info[1] not in langs:
            langs.append(info[1])
    return langs


def _as_list(v) -> list:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [s.strip() for s in re.split(r"[,，;；\n]+", v) if s.strip()]
    return []


def generate_profile(deepseek_key: str, description: str = "", url: str = "",
                     target_countries=None, proxy: str = "") -> dict:
    """
    返回 {"ok": True, "profile": {...}} 或 {"ok": False, "error": "..."}。
    description / url 至少给一个。
    """
    if not deepseek_key:
        return {"ok": False, "error": "请先在系统设置填 DeepSeek API Key"}

    ex = AIExtractor()
    ex.deepseek_key = deepseek_key
    ex.proxy        = proxy

    page = ""
    if url:
        try:
            page = ex.html_to_text(ex.fetch_html(url), max_chars=4000)
        except Exception:
            page = ""

    blob = (f"产品描述：{description}\n\n产品页内容：{page}").strip()
    if not (description.strip() or page):
        return {"ok": False, "error": "请先填产品描述，或贴一个产品页网址"}

    langs = target_langs(target_countries)
    i18n_req = ""
    if langs:
        names = "、".join(_LANG_NAME[l] for l in langs)
        i18n_req = (f"keywords_i18n(把 keywords_en 里最核心的 2-4 个词翻译成目标市场语言，"
                    f"输出对象，键用语言代码 {langs}，每个值是该语言的关键词数组)，覆盖：{names}。\n")

    instruction = (
        "你是资深外贸搜索词专家。下面是一个外贸卖家的产品信息。请提炼一份用于"
        "全网搜索海外买家/经销商的「产品搜索画像」，只输出一个对象的 JSON 数组"
        "（数组里就一个对象），字段：\n"
        "category(产品类目，中文一句话)\n"
        "keywords_en(核心英文/行业术语关键词，买家和经销商真正会用来搜的词，"
        "字符串数组，5-10 个，最重要，要具体可搜，别太宽泛)\n"
        "synonyms(英文同义词或别称，数组)\n"
        "models(主力型号/规格示例，数组，没有就空)\n"
        "applications(典型应用/用途，数组)\n"
        "buyer_types(目标买家类型，从 distributor/wholesaler/importer/"
        "OEM factory/retailer/end user 中选，数组)\n"
        "hs_suggested(可能的 HS 海关编码，数组，不确定就空)\n"
        + i18n_req +
        "只依据给出的信息，关键词务必具体、可直接拿去搜索，不要编造无关词。"
    )

    rows = ex.extract_from_text(blob, instruction, max_tokens=1200)
    prof = rows[0] if rows else {}
    if not isinstance(prof, dict):
        prof = {}

    out = {
        "category":     str(prof.get("category", "")).strip()[:120],
        "keywords_en":  _as_list(prof.get("keywords_en"))[:12],
        "synonyms":     _as_list(prof.get("synonyms"))[:12],
        "models":       _as_list(prof.get("models"))[:10],
        "applications": _as_list(prof.get("applications"))[:8],
        "buyer_types":  _as_list(prof.get("buyer_types"))[:6],
        "hs_suggested": _as_list(prof.get("hs_suggested"))[:8],
        "keywords_i18n": {},
    }
    i18n = prof.get("keywords_i18n") or {}
    if isinstance(i18n, dict):
        for l in langs:
            terms = _as_list(i18n.get(l))[:6]
            if terms:
                out["keywords_i18n"][l] = terms

    if not out["keywords_en"]:
        return {"ok": False, "error": "AI 没能提取出关键词，请把产品描述写具体些再试"}
    return {"ok": True, "profile": out, "langs": langs}
