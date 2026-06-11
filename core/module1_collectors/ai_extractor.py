"""
module1_collectors/ai_extractor.py — AI 网页解析（ScrapeGraphAI 等价实现）
============================================================================
给一个网址 + 一句话说明要什么 → 自动抓网页、去标签、丢给 DeepSeek 提取结构化数据。

ScrapeGraphAI（GitHub ⭐15k+）的核心思路就是「网页 + LLM = 结构化 JSON」。
它本体依赖 playwright/langchain 一大堆库，在 Windows 便携版 Python 上安装沉重；
这里用 curl_cffi（伪装浏览器指纹绕基础反爬）+ 正则去标签 + 你已有的 DeepSeek Key
原生实现同一能力，零额外依赖、即装即用。

用法：
    ex = AIExtractor()
    ex.deepseek_key = cfg.get("deepseek_api_key", "")
    rows = ex.extract(
        url="https://example.com/buyers",
        instruction="提取页面里所有采购商，字段：company_name, country, email, notes",
    )
    # rows 是 list[dict]，每个 dict 就是一条结构化记录

也可不抓网页、直接喂 HTML/文本：
    rows = ex.extract_from_text(text, instruction=...)
"""

import re
import json
import time
import random
from typing import Optional

import requests

try:
    from curl_cffi import requests as cf          # 伪装 Chrome 指纹，绕基础反爬
    _HAS_CF = True
except Exception:                                  # pragma: no cover
    _HAS_CF = False

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/120.0.0.0 Safari/537.36")

# 去掉这些标签连同内容（脚本/样式/导航等噪音）
_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|svg|head|nav|footer|header)[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TAGS = re.compile(r"<[^>]+>")
_WS   = re.compile(r"[ \t\r\f\v]+")
_NL   = re.compile(r"\n\s*\n+")


class AIExtractor:

    def __init__(self):
        self.deepseek_key = ""
        self.timeout      = 25
        self.proxy        = ""     # 可选代理地址(http://user:pass@host:port)；空=直连本机IP
        self.max_retries  = 2      # 遇 403/429/503 时的额外重试次数（带退避）
        self._cache: dict = {}

    # ── 抓网页 ──────────────────────────────────────────────────────────────

    def _proxies(self):
        """有填代理就用，没填返回 None（走本机 IP）。"""
        if self.proxy:
            return {"http": self.proxy, "https": self.proxy}
        return None

    def fetch_html(self, url: str) -> str:
        """
        抓取网页 HTML。优先 curl_cffi 伪装 Chrome，失败回退 requests。
        特性：① 中文站编码自适应(GBK/GB2312/UTF-8)；② 遇反爬限流(403/429/503)
        自动退避重试；③ 可选代理 IP（self.proxy 留空则走本机）。
        """
        if not url:
            return ""
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        proxies = self._proxies()

        # curl_cffi：模拟真实 Chrome TLS 指纹，能过掉一部分 Cloudflare/WAF
        if _HAS_CF:
            for attempt in range(self.max_retries + 1):
                try:
                    kw = dict(impersonate="chrome120", timeout=self.timeout,
                              allow_redirects=True)
                    if proxies:
                        kw["proxies"] = proxies
                    r = cf.get(url, **kw)
                    if r.status_code == 200 and r.content:
                        return self._decode(r)
                    # 被限流/拦截：退避后再试，避免猛刷激怒对方网站
                    if r.status_code in (403, 429, 503) and attempt < self.max_retries:
                        time.sleep(random.uniform(2.0, 4.0) * (attempt + 1))
                        continue
                    break
                except Exception as e:
                    print(f"[AIExtractor] curl_cffi 抓取失败 {url}: {e}")
                    if attempt < self.max_retries:
                        time.sleep(random.uniform(1.5, 3.0))
                        continue
                    break

        # 回退普通 requests
        try:
            kw = dict(headers={"User-Agent": _UA,
                               "Accept-Language": "en-US,en;q=0.9"},
                      timeout=self.timeout, allow_redirects=True)
            if proxies:
                kw["proxies"] = proxies
            r = requests.get(url, **kw)
            if r.status_code == 200:
                return self._decode(r)
        except Exception as e:
            print(f"[AIExtractor] requests 抓取失败 {url}: {e}")
        return ""

    @staticmethod
    def _decode(resp) -> str:
        """
        字节流 → 文本，编码自适应。中文站常用 GBK/GB2312（统一按兼容超集
        gb18030 解码），国外站多为 UTF-8。优先用页面 <meta charset> 声明。
        """
        raw = getattr(resp, "content", None)
        if raw is None:
            return getattr(resp, "text", "") or ""
        if not raw:
            return ""
        # 嗅探 <meta charset>（前 2KB 足够）
        enc = ""
        m = re.search(rb'charset=["\']?\s*([a-z0-9_\-]+)', raw[:2048].lower())
        if m:
            enc = m.group(1).decode("ascii", "ignore").lower()
        # 中文编码统一用 gb18030（兼容 gbk/gb2312）
        if enc in ("gb2312", "gbk", "gb-2312", "gb18030"):
            try:
                return raw.decode("gb18030")
            except Exception:
                pass
        elif enc and enc not in ("utf-8", "utf8"):
            try:
                return raw.decode(enc)
            except Exception:
                pass
        # 默认 utf-8 → 退 gb18030 → 最后用替换兜底（不抛错）
        for e in ("utf-8", "gb18030"):
            try:
                return raw.decode(e)
            except Exception:
                continue
        return raw.decode("utf-8", "replace")

    # ── HTML → 纯文本 ───────────────────────────────────────────────────────

    @staticmethod
    def html_to_text(html: str, max_chars: int = 6000) -> str:
        """把 HTML 压成干净的纯文本，控制长度以节省 token。"""
        if not html:
            return ""
        text = _DROP_BLOCKS.sub(" ", html)
        text = _TAGS.sub(" ", text)
        # 常见 HTML 实体
        for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                     ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
            text = text.replace(a, b)
        text = _WS.sub(" ", text)
        text = _NL.sub("\n", text)
        return text.strip()[:max_chars]

    # ── DeepSeek 结构化提取 ─────────────────────────────────────────────────

    def extract_from_text(self, text: str, instruction: str,
                          max_tokens: int = 1200) -> list[dict]:
        """把文本 + 指令丢给 DeepSeek，返回 list[dict]。"""
        if not text or not self.deepseek_key:
            return []

        prompt = f"""你是一个网页数据提取专家。下面是一段网页正文，请按要求提取结构化数据。

要求：{instruction}

网页正文：
\"\"\"
{text}
\"\"\"

只输出一个 JSON 数组（每个元素是一个对象），不要任何解释文字、不要 markdown 代码块。
如果页面里没有符合要求的数据，输出 []。"""

        try:
            resp = requests.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.deepseek_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": "deepseek-chat",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_tokens": max_tokens,
                },
                timeout=40,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            return self._parse_json_array(content)
        except Exception as e:
            print(f"[AIExtractor] DeepSeek 解析失败: {e}")
            return []

    def extract(self, url: str, instruction: str,
                max_tokens: int = 1200) -> list[dict]:
        """抓网页 + 提取，一步到位。带内存缓存，同 URL 不重复抓。"""
        if url in self._cache:
            html = self._cache[url]
        else:
            html = self.fetch_html(url)
            self._cache[url] = html
            time.sleep(random.uniform(0.15, 0.4))
        if not html:
            return []
        text = self.html_to_text(html)
        return self.extract_from_text(text, instruction, max_tokens=max_tokens)

    # ── 工具：从 LLM 回复里抠出 JSON 数组 ───────────────────────────────────

    @staticmethod
    def _parse_json_array(content: str) -> list[dict]:
        if not content:
            return []
        # 去掉可能的 ```json ... ``` 包裹
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(),
                         flags=re.IGNORECASE | re.MULTILINE).strip()
        # 直接整体解析
        try:
            data = json.loads(content)
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
            if isinstance(data, dict):
                return [data]
        except Exception:
            pass
        # 退一步：抠出第一个 [ ... ] 块
        m = re.search(r"\[.*\]", content, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                if isinstance(data, list):
                    return [d for d in data if isinstance(d, dict)]
            except Exception:
                pass
        return []


# 单例
ai_extractor = AIExtractor()
