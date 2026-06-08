"""
module1_collectors/youtube.py — YouTube 社交媒体获客
======================================================
思路（用户实战经验）：搜关键词 → 点进视频 → 评论区常有人问价/问合作/留邮箱，
视频简介里也常留官网和联系方式。把这些主动表达意向的真实买家挖出来。

两条采集后端，自动选择：
  • 官方 YouTube Data API v3（优先）：配了 youtube_api_key 就用，数据干净、快、额度可控
    （免费额度每天 1 万单位：search 100/次、commentThreads 1/次，够挖很多）
  • yt-dlp（降级）：没有 key 时用，免申请、纯 Python，稍慢

两类线索：
  1. 视频简介里的联系方式 → 多是卖家/经销商/同行（潜在伙伴或对标）
  2. 评论区高意向留言（问价/求合作/留邮箱）→ 多是真实买家

用法（在 app.py run_bg 里）：
    yc = YouTubeCollector()
    yc.api_key = cfg.get("youtube_api_key", "")
    yc.product_name = cfg.get("product_name", "")
    yc.search_keywords = cfg.get("search_keywords", [])
    leads = yc.fetch_all()
"""

import re
import time
import random
import requests

try:
    import yt_dlp
    _HAS_YTDLP = True
except Exception:                                  # pragma: no cover
    _HAS_YTDLP = False

API_BASE = "https://www.googleapis.com/youtube/v3"

# ── 正则 ────────────────────────────────────────────────────────────────────
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<![\w])(\+?\d[\d\s().\-]{7,16}\d)(?![\w])")
_SITE_RE  = re.compile(r"https?://[A-Za-z0-9.\-]+\.[A-Za-z]{2,}(?:/[^\s)]*)?")
_WA_RE    = re.compile(r"(?:wa\.me/|whatsapp[^\d]{0,6})(\+?\d[\d\s\-]{7,15})", re.I)

_EMAIL_JUNK = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")
_SITE_SKIP  = ("youtube.com", "youtu.be", "instagram.com", "facebook.com",
               "twitter.com", "x.com", "tiktok.com", "bit.ly", "goo.gl",
               "linktr.ee", "t.me")

# 采购意向关键词（命中越多分越高）
_INTENT = {
    "price": ["price", "pricing", "how much", "cost", "quote", "quotation",
              "价格", "多少钱", "报价"],
    "deal":  ["cooperat", "partner", "distributor", "dealer", "wholesale",
              "supplier", "import", "bulk", "moq", "order", "buy", "purchase",
              "interested", "agent", "代理", "批发", "合作", "进口"],
    "contact": ["whatsapp", "email", "contact", "dm", "reach", "邮箱", "联系"],
}


def _clean_emails(text: str) -> set:
    out = set()
    deob = (text or "").replace("[at]", "@").replace("(at)", "@").replace(" at ", "@")
    for m in _EMAIL_RE.findall(deob):
        em = m.strip().strip(".").lower()
        if em.endswith(_EMAIL_JUNK) or len(em) > 60 or em.count("@") != 1:
            continue
        out.add(em)
    return out


def _clean_phones(text: str) -> set:
    out = set()
    for m in _WA_RE.findall(text or ""):
        d = re.sub(r"\D", "", m)
        if 8 <= len(d) <= 15:
            out.add("+" + d if not m.strip().startswith("+") else m.strip())
    for m in _PHONE_RE.findall(text or ""):
        d = re.sub(r"\D", "", m)
        if 9 <= len(d) <= 15:
            out.add(m.strip())
    return out


def _first_site(text: str) -> str:
    for m in _SITE_RE.findall(text or ""):
        low = m.lower()
        if not any(s in low for s in _SITE_SKIP):
            return m.rstrip(".,)")
    return ""


def _intent_score(text: str):
    t = (text or "").lower()
    hits, labels = 0, []
    for label, kws in _INTENT.items():
        if any(k in t for k in kws):
            hits += 1
            labels.append(label)
    return hits, labels


class YouTubeCollector:

    def __init__(self):
        self.api_key         = ""
        self.deepseek_key    = ""        # 预留：可用 DeepSeek 进一步判意向
        self.product_name    = ""
        self.search_keywords = []
        self.max_videos      = 8
        self.max_comments    = 60
        self._seen           = set()

    # ── 搜索词 ──────────────────────────────────────────────────────────────
    def _queries(self) -> list:
        kws = self.search_keywords[:3] if self.search_keywords else \
              ([self.product_name] if self.product_name else ["motorcycle engine"])
        qs = []
        for kw in kws:
            qs += [f"{kw} supplier", f"{kw} review", kw]
        seen, out = set(), []
        for q in qs:
            if q not in seen:
                seen.add(q); out.append(q)
        return out[:4]

    # ── 后端 A：官方 API ────────────────────────────────────────────────────
    def _api_search(self, query: str) -> list:
        try:
            r = requests.get(f"{API_BASE}/search", params={
                "part": "snippet", "q": query, "type": "video",
                "maxResults": self.max_videos, "key": self.api_key,
                "relevanceLanguage": "en"}, timeout=20)
            if r.status_code != 200:
                print(f"[YouTube] API search {r.status_code}: {r.text[:120]}")
                return []
            out = []
            for it in r.json().get("items", []):
                vid = it.get("id", {}).get("videoId")
                if vid:
                    out.append({"id": vid,
                                "title": it.get("snippet", {}).get("title", ""),
                                "channel": it.get("snippet", {}).get("channelTitle", "")})
            return out
        except Exception as e:
            print(f"[YouTube] API search 异常: {e}")
            return []

    def _api_video_desc(self, video_ids: list) -> dict:
        if not video_ids:
            return {}
        try:
            r = requests.get(f"{API_BASE}/videos", params={
                "part": "snippet", "id": ",".join(video_ids), "key": self.api_key},
                timeout=20)
            if r.status_code != 200:
                return {}
            out = {}
            for it in r.json().get("items", []):
                sn = it.get("snippet", {})
                out[it.get("id")] = {"description": sn.get("description", ""),
                                     "title": sn.get("title", ""),
                                     "channel": sn.get("channelTitle", "")}
            return out
        except Exception:
            return {}

    def _api_comments(self, video_id: str) -> list:
        try:
            r = requests.get(f"{API_BASE}/commentThreads", params={
                "part": "snippet", "videoId": video_id, "maxResults": 100,
                "order": "relevance", "textFormat": "plainText",
                "key": self.api_key}, timeout=20)
            if r.status_code != 200:
                return []
            out = []
            for it in r.json().get("items", []):
                c = it.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
                out.append({"text": c.get("textDisplay", ""),
                            "author": c.get("authorDisplayName", ""),
                            "channel_url": c.get("authorChannelUrl", "")})
            return out
        except Exception:
            return []

    # ── 后端 B：yt-dlp ──────────────────────────────────────────────────────
    def _ytdlp_search_ids(self, query: str) -> list:
        if not _HAS_YTDLP:
            return []
        try:
            opts = {"quiet": True, "skip_download": True, "extract_flat": "in_playlist",
                    "noplaylist": False, "ignoreerrors": True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{self.max_videos}:{query}", download=False)
            out = []
            for e in (info or {}).get("entries", []) or []:
                if e and e.get("id"):
                    out.append({"id": e["id"], "title": e.get("title", ""),
                                "channel": e.get("channel", "") or e.get("uploader", "")})
            return out
        except Exception as e:
            print(f"[YouTube] yt-dlp search 异常: {e}")
            return []

    def _ytdlp_video(self, video_id: str) -> dict:
        if not _HAS_YTDLP:
            return {}
        try:
            opts = {"quiet": True, "skip_download": True, "getcomments": True,
                    "ignoreerrors": True,
                    "extractor_args": {"youtube": {"max_comments": [str(self.max_comments)],
                                                   "comment_sort": ["top"]}}}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}",
                                        download=False)
            if not info:
                return {}
            comments = [{"text": c.get("text", ""), "author": c.get("author", ""),
                         "channel_url": c.get("author_url", "")}
                        for c in (info.get("comments") or [])]
            return {"description": info.get("description", "") or "",
                    "title": info.get("title", ""),
                    "channel": info.get("channel", "") or info.get("uploader", ""),
                    "comments": comments}
        except Exception as e:
            print(f"[YouTube] yt-dlp video 异常: {e}")
            return {}

    # ── 简介 → 线索 ─────────────────────────────────────────────────────────
    def _lead_from_desc(self, desc: str, title: str, channel: str) -> dict:
        emails = _clean_emails(desc)
        phones = _clean_phones(desc)
        site   = _first_site(desc)
        if not (emails or phones or site):
            return None
        name = channel or "YouTube Channel"
        return {
            "company_name": name[:80],
            "country": "",
            "website": site,
            "email": sorted(emails)[0] if emails else "",
            "phone": sorted(phones)[0] if phones else "",
            "contact_name": "",
            "notes": f"[YouTube视频简介] {title[:80]}"
                     + (f" | 其他邮箱:{','.join(sorted(emails)[1:3])}" if len(emails) > 1 else ""),
            "sources": ["youtube"],
            "status": "new",
        }

    # ── 评论 → 线索 ─────────────────────────────────────────────────────────
    def _lead_from_comment(self, c: dict, title: str, video_id: str) -> dict:
        text = c.get("text", "")
        emails = _clean_emails(text)
        phones = _clean_phones(text)
        score, labels = _intent_score(text)
        # 只要：有邮箱/电话，或意向命中>=2类（如 价格+合作）
        if not (emails or phones or score >= 2):
            return None
        author = (c.get("author") or "").lstrip("@") or "YouTube Buyer"
        vlink  = f"https://youtu.be/{video_id}"
        note = f"[YouTube评论·{'/'.join(labels) or '高意向'}] {text[:160]} | 视频:{title[:50]} {vlink}"
        if c.get("channel_url"):
            note += f" | 主页:{c['channel_url']}"
        return {
            "company_name": author[:80],
            "country": "",
            "website": "",
            "email": sorted(emails)[0] if emails else "",
            "phone": sorted(phones)[0] if phones else "",
            "contact_name": author[:80],
            "notes": note[:480],
            "sources": ["youtube"],
            "status": "new",
        }

    # ── 主入口 ──────────────────────────────────────────────────────────────
    def fetch_all(self, mock: bool = False) -> list:
        if mock:
            return self._mock()
        use_api = bool(self.api_key)
        if not use_api and not _HAS_YTDLP:
            print("[YouTube] 既没有 API key 也没装 yt-dlp，跳过")
            return []

        leads = []
        for query in self._queries():
            videos = self._api_search(query) if use_api else self._ytdlp_search_ids(query)
            if not videos:
                continue
            print(f"[YouTube] '{query}' → {len(videos)} 个视频（{'API' if use_api else 'yt-dlp'}）")

            if use_api:
                details = self._api_video_desc([v["id"] for v in videos])
                for v in videos:
                    d = details.get(v["id"], {})
                    desc = d.get("description", "")
                    title = d.get("title", v["title"])
                    lead = self._lead_from_desc(desc, title, d.get("channel", v["channel"]))
                    if lead:
                        self._add(leads, lead)
                    for c in self._api_comments(v["id"])[:self.max_comments]:
                        cl = self._lead_from_comment(c, title, v["id"])
                        if cl:
                            self._add(leads, cl)
                    time.sleep(random.uniform(0.2, 0.5))
            else:
                for v in videos:
                    info = self._ytdlp_video(v["id"])
                    if not info:
                        continue
                    title = info.get("title", v["title"])
                    lead = self._lead_from_desc(info.get("description", ""), title,
                                                info.get("channel", v["channel"]))
                    if lead:
                        self._add(leads, lead)
                    for c in info.get("comments", [])[:self.max_comments]:
                        cl = self._lead_from_comment(c, title, v["id"])
                        if cl:
                            self._add(leads, cl)
                    time.sleep(random.uniform(0.3, 0.7))

        print(f"[YouTube] 共挖出 {len(leads)} 条线索")
        return leads

    def _add(self, leads: list, lead: dict):
        key = (lead.get("email") or lead.get("phone")
               or lead.get("company_name", "")).lower()
        if key and key not in self._seen:
            self._seen.add(key)
            leads.append(lead)

    def _mock(self) -> list:
        return [
            {"company_name": "Lagos Bike Traders", "country": "", "website": "https://lagosbike.ng",
             "email": "sales@lagosbike.ng", "phone": "", "contact_name": "",
             "notes": "[YouTube视频简介] CG150 Engine Review", "sources": ["youtube"], "status": "new"},
            {"company_name": "Ahmed K", "country": "", "website": "",
             "email": "ahmed.buyer@gmail.com", "phone": "+2348011112222",
             "contact_name": "Ahmed K",
             "notes": "[YouTube评论·price/deal] how much for 200 units? interested to be distributor in Nigeria",
             "sources": ["youtube"], "status": "new"},
        ]


youtube_collector = YouTubeCollector()
