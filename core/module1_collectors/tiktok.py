"""
module1_collectors/tiktok.py — TikTok 社交媒体获客（通过 Apify 付费抓取服务）
================================================================================
TikTok 没有可用的官方评论接口，自己硬爬反爬极凶、违反条款、封号风险高，
不适合放进对外卖的 SaaS。所以这里接 Apify（专业抓取托管平台，它替你扛反爬），
客户在设置里填自己的 Apify Token 即可启用，按量付费。

两步：
  1. 关键词搜视频（video_actor）→ 拿到视频文案(caption)、作者、视频URL
     → 从文案/作者签名里提联系方式（卖家/经销商，潜在伙伴或对标）
  2. 对搜到的视频抓评论（comment_actor）→ 评论里问价/求合作/留邮箱的 = 真实买家
     → 提邮箱电话 + 判采购意向

字段提取做成「容错」：不同 Apify actor 返回字段名不一样，这里递归扫描每个条目的
所有文本值跑正则，再从常见 name 字段取联系人名，兼容多种 actor。

用法（app.py run_bg）：
    tc = TikTokCollector()
    tc.apify_token = cfg.get("apify_token", "")
    tc.product_name = cfg.get("product_name", "")
    tc.search_keywords = cfg.get("search_keywords", [])
    leads = tc.fetch_all()
"""

import re
import requests

from module1_collectors.youtube import (_clean_emails, _clean_phones,
                                         _first_site, _intent_score)

APIFY_BASE = "https://api.apify.com/v2/acts"

# 常见的「作者名/昵称」字段（按优先级）
_NAME_KEYS = ("nickName", "nickname", "name", "uniqueId", "authorName",
              "author", "userName", "username")
# 常见的「文案/评论正文」字段
_TEXT_KEYS = ("text", "desc", "caption", "title", "comment", "content", "signature")


def _walk_strings(obj, acc: list, depth: int = 0):
    """递归收集 dict/list 里的所有字符串值（限制深度，避免爆炸）。"""
    if depth > 4:
        return
    if isinstance(obj, str):
        if obj:
            acc.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk_strings(v, acc, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:30]:
            _walk_strings(v, acc, depth + 1)


def _find_name(item: dict) -> str:
    # 先在顶层和 authorMeta/author 里找
    for container in (item, item.get("authorMeta") or {}, item.get("author") or {}):
        if isinstance(container, dict):
            for k in _NAME_KEYS:
                v = container.get(k)
                if v and isinstance(v, str):
                    return v.strip().lstrip("@")[:80]
    return ""


def _find_text(item: dict) -> str:
    for k in _TEXT_KEYS:
        v = item.get(k)
        if v and isinstance(v, str):
            return v
    return ""


def _find_url(item: dict) -> str:
    for k in ("webVideoUrl", "videoUrl", "postPage", "url", "shareUrl"):
        v = item.get(k)
        if v and isinstance(v, str) and "tiktok" in v.lower():
            return v
    return ""


class TikTokCollector:

    def __init__(self):
        self.apify_token     = ""
        self.product_name    = ""
        self.search_keywords = []
        self.max_videos      = 10
        self.fetch_comments  = True
        self.max_comments    = 40
        # 可在设置里覆盖成别的 actor
        self.video_actor     = "clockworks~free-tiktok-scraper"
        self.comment_actor   = "clockworks~tiktok-comments-scraper"
        self._seen           = set()

    def _terms(self) -> list:
        kws = self.search_keywords[:3] if self.search_keywords else \
              ([self.product_name] if self.product_name else ["motorcycle engine"])
        return kws

    # ── Apify 调用 ──────────────────────────────────────────────────────────
    def _run_actor(self, actor: str, payload: dict) -> list:
        """同步运行 actor 并直接取数据集条目。"""
        url = f"{APIFY_BASE}/{actor}/run-sync-get-dataset-items"
        try:
            r = requests.post(url, params={"token": self.apify_token},
                              json=payload, timeout=300)
            if r.status_code in (200, 201):
                data = r.json()
                return data if isinstance(data, list) else []
            print(f"[TikTok] Apify {actor} 返回 {r.status_code}: {r.text[:160]}")
            return []
        except Exception as e:
            print(f"[TikTok] Apify {actor} 异常: {e}")
            return []

    # ── 条目 → 线索 ─────────────────────────────────────────────────────────
    def _lead_from_item(self, item: dict, kind: str) -> dict:
        strings = []
        _walk_strings(item, strings)
        blob = "\n".join(strings)
        emails = _clean_emails(blob)
        phones = _clean_phones(blob)
        text   = _find_text(item)
        name   = _find_name(item)
        url    = _find_url(item)

        if kind == "comment":
            score, labels = _intent_score(text or blob)
            if not (emails or phones or score >= 2):
                return None
            tag = "/".join(labels) or "高意向"
            note = f"[TikTok评论·{tag}] {(text or '')[:160]}"
            company = name or "TikTok Buyer"
        else:  # video
            site = _first_site(blob)
            if not (emails or phones):
                return None       # 视频没留联系方式就不要（避免噪音）
            note = f"[TikTok视频] {(text or '')[:120]}"
            company = name or "TikTok Seller"
            if site and not emails:
                pass
        if url:
            note += f" | {url}"

        return {
            "company_name": company[:80],
            "country": "",
            "website": _first_site(blob) if kind == "video" else "",
            "email": sorted(emails)[0] if emails else "",
            "phone": sorted(phones)[0] if phones else "",
            "contact_name": name[:80] if name else "",
            "notes": note[:480],
            "sources": ["tiktok"],
            "status": "new",
        }

    def _add(self, leads: list, lead: dict):
        key = (lead.get("email") or lead.get("phone")
               or lead.get("company_name", "")).lower()
        if key and key not in self._seen:
            self._seen.add(key)
            leads.append(lead)

    # ── 主入口 ──────────────────────────────────────────────────────────────
    def fetch_all(self, mock: bool = False) -> list:
        if mock:
            return self._mock()
        if not self.apify_token:
            print("[TikTok] 未配置 Apify Token，跳过")
            return []

        leads = []
        # 1. 搜视频
        videos = self._run_actor(self.video_actor, {
            "searchQueries": self._terms(),
            "resultsPerPage": self.max_videos,
            "shouldDownloadVideos": False,
            "shouldDownloadCovers": False,
            "shouldDownloadSubtitles": False,
        })
        print(f"[TikTok] 搜到 {len(videos)} 条视频")
        video_urls = []
        for it in videos:
            u = _find_url(it)
            if u:
                video_urls.append(u)
            lead = self._lead_from_item(it, "video")
            if lead:
                self._add(leads, lead)

        # 2. 抓评论
        if self.fetch_comments and video_urls:
            comments = self._run_actor(self.comment_actor, {
                "postURLs": video_urls[:self.max_videos],
                "commentsPerPost": self.max_comments,
                "maxComments": self.max_comments * len(video_urls[:self.max_videos]),
            })
            print(f"[TikTok] 抓到 {len(comments)} 条评论")
            for c in comments:
                lead = self._lead_from_item(c, "comment")
                if lead:
                    self._add(leads, lead)

        print(f"[TikTok] 共挖出 {len(leads)} 条线索")
        return leads

    def _mock(self) -> list:
        return [
            {"company_name": "MotoParts Vietnam", "country": "", "website": "https://motoparts.vn",
             "email": "import@motoparts.vn", "phone": "", "contact_name": "",
             "notes": "[TikTok视频] best 150cc engine for our market", "sources": ["tiktok"], "status": "new"},
            {"company_name": "rajesh_imports", "country": "", "website": "",
             "email": "", "phone": "+923001234567", "contact_name": "rajesh_imports",
             "notes": "[TikTok评论·price/deal] price for 500pcs? we are distributor in Karachi, whatsapp me",
             "sources": ["tiktok"], "status": "new"},
        ]


tiktok_collector = TikTokCollector()
