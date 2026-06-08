# -*- coding: utf-8 -*-
"""
pipeline_config.py — 数据流水线配置（算法切换接口）
=====================================================
这是整个获客流水线的「单一事实来源」。每个阶段用了哪个模块/算法、对标哪个
GitHub 项目、是否需要配置、能否切换算法，全部在这里描述。

为什么单独抽一个配置文件：
  - 管理后台「技术架构」页直接读它来渲染，改算法说明只改这一处。
  - 每个 step 的结构就是「接口」：将来想把某个等价实现换成 GitHub 原版，
    或换更好的算法，只要改这里的 file/algorithm/status，再去对应模块替换实现即可。

status 取值（决定徽章颜色）：
  active        运行中（已能用，可能需要填 Key 但有免费降级）
  needs_config  需要配置 API Key 才能跑真实数据
  overseas_only 需部署到境外服务器才稳定（境内 IP 被反爬墙）
  todo          界面/接口已留，代码待写
"""

# 徽章样式（深色后台用）
STATUS_META = {
    "active":        {"label": "运行中",   "color": "#34d399", "bg": "#064e3b", "dot": "#10b981"},
    "needs_config":  {"label": "需配置Key", "color": "#fbbf24", "bg": "#451a03", "dot": "#f59e0b"},
    "overseas_only": {"label": "需境外节点", "color": "#60a5fa", "bg": "#1e3a5f", "dot": "#3b82f6"},
    "todo":          {"label": "待开发",   "color": "#94a3b8", "bg": "#1e293b", "dot": "#64748b"},
}


def _gh(name, url, stars, type_="inspiration", note=""):
    return {"name": name, "url": url, "stars": stars, "type": type_, "note": note}


PIPELINE_STAGES = [
    # ── 阶段 1：发现线索 ────────────────────────────────────────────────
    {
        "num": 1, "id": "discover", "icon": "🔍", "name": "发现线索",
        "desc": "从海关数据、地图、搜索引擎、B2B平台、社媒等多渠道找到潜在买家",
        "steps": [
            {
                "id": "importyeti", "name": "ImportYeti 美国海关", "desc": "美国进口记录，找真实从中国采购的买家",
                "status": "needs_config", "file": "core/module1_collectors/importyeti.py",
                "algorithm": "ImportYeti API（有Key走API，无Key走免费网页解析）",
                "github": [_gh("ImportYeti", "https://www.importyeti.com", "—", "service")],
                "alternatives": ["Panjiva / ImportGenius（付费海关数据）"], "configurable": True,
            },
            {
                "id": "zauba", "name": "Zauba 印度海关", "desc": "印度进口商数据，完全免费",
                "status": "overseas_only", "file": "core/module1_collectors/zauba.py",
                "algorithm": "curl_cffi 伪装 Chrome TLS 指纹 + 正则解析，失败降级模拟数据",
                "github": [_gh("botasaurus", "https://github.com/omkarcloud/botasaurus", "5.4k",
                               "inspiration", "等价实现，境外服务器部署后可切真实版")],
                "alternatives": ["botasaurus 原版（需境外服务器 + pip install botasaurus）"], "configurable": True,
            },
            {
                "id": "alibaba_rfq", "name": "阿里巴巴 RFQ", "desc": "全球买家公开发布的采购需求",
                "status": "overseas_only", "file": "core/module1_collectors/alibaba_rfq.py",
                "algorithm": "curl_cffi 抓页 + DeepSeek 智能解析（crawlee 等价），失败退正则再退模拟",
                "github": [_gh("crawlee-python", "https://github.com/apify/crawlee-python", "5k",
                               "inspiration", "JS渲染页面采集，本系统用 curl_cffi+AI 等价")],
                "alternatives": ["crawlee + Playwright（需浏览器内核）"], "configurable": True,
            },
            {
                "id": "europages", "name": "Europages 欧洲B2B", "desc": "欧洲29国B2B企业目录",
                "status": "overseas_only", "file": "core/module1_collectors/europages.py",
                "algorithm": "curl_cffi 伪装指纹 + 正则解析，失败降级模拟数据",
                "github": [_gh("botasaurus", "https://github.com/omkarcloud/botasaurus", "5.4k", "inspiration")],
                "alternatives": ["botasaurus 原版"], "configurable": True,
            },
            {
                "id": "google_search", "name": "Google 搜索 + AI", "desc": "搜索引擎发现未被海关记录的买家",
                "status": "needs_config", "file": "core/module1_collectors/google_search.py",
                "algorithm": "Serper.dev 谷歌搜索 API + DeepSeek 判断是否真实进口商并提取信息",
                "github": [_gh("Serper", "https://serper.dev", "—", "service")],
                "alternatives": ["SerpAPI / 自建 SearXNG"], "configurable": True,
            },
            {
                "id": "google_maps", "name": "Google Maps 商家", "desc": "地图商家，直接出电话号码",
                "status": "needs_config", "file": "core/module1_collectors/google_maps.py",
                "algorithm": "Serper /maps API（与搜索共用 serpapi_key）",
                "github": [_gh("google-maps-scraper", "https://github.com/gosom/google-maps-scraper", "5k",
                               "inspiration", "已以 Serper Maps API 形式集成")],
                "alternatives": ["gosom/google-maps-scraper 原版"], "configurable": True,
            },
            {
                "id": "apollo", "name": "Apollo.io 联系人", "desc": "2.7亿联系人库，找采购负责人",
                "status": "needs_config", "file": "core/module1_collectors/apollo.py",
                "algorithm": "Apollo.io People Search API",
                "github": [_gh("Apollo.io", "https://apollo.io", "—", "service")],
                "alternatives": ["Lusha / RocketReach"], "configurable": True,
            },
            {
                "id": "youtube", "name": "YouTube 评论 & 简介", "desc": "评论区问价/求合作的买家 + 简介里的联系方式",
                "status": "active", "file": "core/module1_collectors/youtube.py",
                "algorithm": "官方 YouTube Data API 优先；无 Key 自动降级 yt-dlp。正则提邮箱+意向词判定",
                "github": [_gh("yt-dlp", "https://github.com/yt-dlp/yt-dlp", "80k", "library",
                               "免 key 抓简介+评论的降级后端"),
                           _gh("youtube-comment-downloader", "https://github.com/egbertbouman/youtube-comment-downloader", "4k", "inspiration")],
                "alternatives": ["纯官方 API / Apify YouTube Scraper"], "configurable": True,
            },
            {
                "id": "tiktok", "name": "TikTok 评论 & 视频", "desc": "评论区问价的买家 + 文案里的联系方式",
                "status": "needs_config", "file": "core/module1_collectors/tiktok.py",
                "algorithm": "Apify 托管抓取（TikTok 反爬凶，无官方接口，走付费平台扛反爬）",
                "github": [_gh("Apify TikTok Scraper", "https://apify.com/clockworks/tiktok-scraper", "—", "service"),
                           _gh("TikTokApi", "https://github.com/davidteather/TikTok-Api", "12k", "inspiration",
                               "非官方库，需Playwright+token、易失效，不用于生产")],
                "alternatives": ["TikTokApi（自托管，封号风险高）"], "configurable": True,
            },
            {
                "id": "inbound", "name": "独立站询盘", "desc": "客户官网询盘表单提交，自动入库（被动获客）",
                "status": "active", "file": "web/app.py · /api/inbound/<token>",
                "algorithm": "每租户专属 token + 可嵌入 JS 片段 + 公开接收 API（CORS+限流）",
                "github": [], "alternatives": ["Tally / Typeform Webhook"], "configurable": False,
            },
            {
                "id": "facebook", "name": "Facebook 商业主页", "desc": "公开商业主页信息采集",
                "status": "todo", "file": "（界面已有，代码待写）",
                "algorithm": "待实现（有封 IP 风险，建议配合手动群组指南）",
                "github": [], "alternatives": ["手动群组获客指南（已内置）"], "configurable": False,
            },
        ],
    },
    # ── 阶段 2：补充联系方式 ────────────────────────────────────────────
    {
        "num": 2, "id": "enrich", "icon": "📧", "name": "补充联系方式",
        "desc": "给只有公司名/官网的线索，挖出邮箱、电话、社交账号",
        "steps": [
            {
                "id": "email_enricher", "name": "一键找邮箱", "desc": "官网爬取 + 搜索引擎挖掘 + Hunter 三路找邮箱",
                "status": "active", "file": "core/module1_collectors/email_enricher.py",
                "algorithm": "theHarvester + Photon 等价：爬官网各页 + Serper 搜「@域名」+ Hunter API，合并去重按可信度排序",
                "github": [_gh("theHarvester", "https://github.com/laramies/theHarvester", "16k", "inspiration",
                               "搜索引擎挖邮箱，本系统原生等价实现"),
                           _gh("Photon", "https://github.com/s0md3v/Photon", "12k", "inspiration",
                               "爬官网提联系方式，已并入本模块")],
                "alternatives": ["theHarvester/Photon 原版 CLI（重依赖）"], "configurable": True,
            },
            {
                "id": "hunter", "name": "Hunter.io 域名查邮箱", "desc": "按公司域名查公开邮箱+职位",
                "status": "needs_config", "file": "core/module1_collectors/linkedin.py",
                "algorithm": "Hunter.io domain-search API（免费50次/月）",
                "github": [_gh("Hunter.io", "https://hunter.io", "—", "service")],
                "alternatives": ["Snov.io / FindThatLead"], "configurable": True,
            },
            {
                "id": "ai_extractor", "name": "AI 网页解析", "desc": "任意网页 → DeepSeek 提取结构化数据",
                "status": "needs_config", "file": "core/module1_collectors/ai_extractor.py",
                "algorithm": "ScrapeGraphAI 等价：curl_cffi 抓页 + 去标签 + DeepSeek 按 schema 提取",
                "github": [_gh("ScrapeGraphAI", "https://github.com/ScrapeGraphAI/Scrapegraph-ai", "15k",
                               "inspiration", "网页+LLM=结构化，本系统用 curl_cffi+DeepSeek 等价")],
                "alternatives": ["ScrapeGraphAI 原版（重依赖）"], "configurable": True,
            },
        ],
    },
    # ── 阶段 3：验证真实性 ──────────────────────────────────────────────
    {
        "num": 3, "id": "verify", "icon": "✅", "name": "验证真实性",
        "desc": "核实公司是否真实存在、邮箱是否能收信，过滤虚假/死线索",
        "steps": [
            {
                "id": "company_verifier", "name": "公司真实性验证", "desc": "网站存活 + SSL证书 + 域名注册年龄 → 0~100可信度",
                "status": "active", "file": "core/module1_collectors/company_verifier.py",
                "algorithm": "web-check 等价：HTTP存活 + SSL证书颁发/到期 + RDAP域名注册日期（全免费无需Key）",
                "github": [_gh("web-check", "https://github.com/Lissy93/web-check", "24k", "inspiration",
                               "网站体检，本系统挑核实外贸客户最有用的几项原生实现")],
                "alternatives": ["web-check 原版（Node 全栈）"], "configurable": True,
            },
            {
                "id": "email_verifier", "name": "邮箱真伪验证", "desc": "发信前过滤死邮箱，保护发信信誉",
                "status": "active", "file": "core/email_verifier.py",
                "algorithm": "语法 + 一次性域名 + MX记录(DNS-over-HTTPS) + SMTP探测（云上25端口被封时超时算未知不误杀）",
                "github": [], "alternatives": ["ZeroBounce / NeverBounce（付费更准）"], "configurable": True,
            },
        ],
    },
    # ── 阶段 4：AI评分打分 ──────────────────────────────────────────────
    {
        "num": 4, "id": "score", "icon": "⭐", "name": "AI评分打分",
        "desc": "按行业匹配度、采购意向、公司规模等给线索打分分级（A/B/C/D）",
        "steps": [
            {
                "id": "scorer", "name": "线索评分分级", "desc": "规则评分（免费）+ 可选 AI 加权",
                "status": "active", "file": "core/module3_scorer.py",
                "algorithm": "规则引擎（关键词/国家/进口次数等）打基础分；配 DeepSeek 后对高分线索 AI 复核加权",
                "github": [], "alternatives": ["纯规则 / 接入更强 LLM 重排"], "configurable": True,
            },
        ],
    },
    # ── 阶段 5：深度调查 ────────────────────────────────────────────────
    {
        "num": 5, "id": "investigate", "icon": "🕵️", "name": "深度调查",
        "desc": "对高价值客户做背调：聚合验真 + 联系方式 + LinkedIn决策人 + 网络提及",
        "steps": [
            {
                "id": "osint", "name": "高价值客户深度调查", "desc": "一键聚合一家公司的公开情报，生成调查报告",
                "status": "needs_config", "file": "core/module1_collectors/osint_investigator.py",
                "algorithm": "spiderfoot 等价：聚合 company_verifier + email_enricher + Serper搜LinkedIn公开档案 + 网络提及",
                "github": [_gh("SpiderFoot", "https://github.com/smicallef/spiderfoot", "14k", "inspiration",
                               "OSINT聚合，本系统挑外贸背调最有用的几项原生实现"),
                           _gh("linkedin_scraper", "https://github.com/joeyism/linkedin_scraper", "4k", "inspiration",
                               "找决策人，本系统改用 Serper 搜公开档案，合规不登录")],
                "alternatives": ["SpiderFoot 原版 / Proxycurl 付费 LinkedIn API"], "configurable": True,
            },
        ],
    },
    # ── 阶段 6：触达发信 ────────────────────────────────────────────────
    {
        "num": 6, "id": "outreach", "icon": "✉️", "name": "触达发信",
        "desc": "给买家发开发信、WhatsApp，追踪打开，没回复自动跟进",
        "steps": [
            {
                "id": "tenant_mailer", "name": "双通道发信", "desc": "邮箱SMTP 或 专业ESP 二选一发开发信",
                "status": "needs_config", "file": "core/tenant_mailer.py",
                "algorithm": "SMTP（QQ/企业邮箱/Gmail/阿里云）或 ESP（SendGrid/Mailgun，送达率高+自带追踪）",
                "github": [_gh("SendGrid", "https://sendgrid.com", "—", "service"),
                           _gh("Mailgun", "https://mailgun.com", "—", "service")],
                "alternatives": ["Amazon SES / 阿里云邮件推送"], "configurable": True,
            },
            {
                "id": "tracking", "name": "邮件打开追踪", "desc": "知道客户看没看开发信、点没点链接",
                "status": "active", "file": "web/app.py · /t/o /t/c · admin_db.email_tracking",
                "algorithm": "SMTP通道注入1x1追踪像素+改写链接；ESP用其自带追踪。需部署后公网域名生效",
                "github": [], "alternatives": ["ESP 原生追踪"], "configurable": False,
            },
            {
                "id": "followup", "name": "自动跟进序列", "desc": "首封没回复，到点自动发第二/三封",
                "status": "active", "file": "web/app.py · followups表 + 后台调度线程",
                "algorithm": "后台定时扫描 followups，到期用跟进模板自动发；客户标记已回复/成交/拒绝即停止",
                "github": [], "alternatives": ["接入营销自动化平台"], "configurable": False,
            },
            {
                "id": "whatsapp", "name": "WhatsApp 触达", "desc": "B方案手动点击发(免费) + A方案官方API自动发",
                "status": "active", "file": "core/whatsapp_sender.py",
                "algorithm": "B：生成 wa.me 预填话术链接手动发（零成本零封号）；A：Twilio/360dialog/Meta Cloud 官方API自动发",
                "github": [_gh("Twilio WhatsApp", "https://twilio.com/whatsapp", "—", "service"),
                           _gh("WhatsApp Cloud API", "https://developers.facebook.com/docs/whatsapp", "—", "service")],
                "alternatives": ["360dialog / 各 BSP 服务商"], "configurable": True,
            },
        ],
    },
]


def pipeline_summary():
    """租户侧简版用：返回每个阶段的 icon/名称/可用渠道数（不含技术细节）。"""
    out = []
    for st in PIPELINE_STAGES:
        usable = sum(1 for s in st["steps"] if s["status"] != "todo")
        out.append({"num": st["num"], "icon": st["icon"], "name": st["name"],
                    "desc": st["desc"], "count": usable, "total": len(st["steps"])})
    return out


def pipeline_counts():
    """统计各状态数量，给后台页头部用。"""
    c = {"active": 0, "needs_config": 0, "overseas_only": 0, "todo": 0, "total": 0}
    for st in PIPELINE_STAGES:
        for s in st["steps"]:
            c[s["status"]] = c.get(s["status"], 0) + 1
            c["total"] += 1
    return c
