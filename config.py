"""
config.py — 系统全局配置
=========================
所有敏感值从环境变量读取，本文件不含任何密钥，可安全提交 GitHub。

- 本地开发：在项目根目录放一个 .env 文件（已 gitignore，不会上传），写真实值。
- 生产部署（Zeabur / 阿里云）：在平台后台填环境变量，无需 .env。

需要的环境变量：
  SMTP_USER / SMTP_PASS   平台系统邮件账号（注册验证、密码重置用）
  SITE_URL                部署后的公网域名，如 https://leadflow.yourdomain.com
                          —— 邮件里的链接、邮件打开追踪像素都用它，必须设对
  SECRET_KEY              Flask session 密钥（随便一段长随机字符串，固定不变）
  DATA_DIR                数据存放目录（数据库、租户数据），默认项目根目录；
                          云上挂载持久卷时指向卷目录，如 /data
"""
import os
from pathlib import Path

# 本地开发：若存在 .env 则加载进环境变量（不覆盖已存在的真实环境变量）
_envfile = Path(__file__).parent / ".env"
if _envfile.exists():
    for _line in _envfile.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

SMTP_HOST      = os.environ.get("SMTP_HOST", "smtp.qq.com")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER      = os.environ.get("SMTP_USER", "")
SMTP_PASS      = os.environ.get("SMTP_PASS", "")
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "LeadFlow")

# 部署后改成真实域名（去掉结尾斜杠）。本地默认 127.0.0.1
SITE_URL = os.environ.get("SITE_URL", "http://127.0.0.1:5001").rstrip("/")

# ── 平台代付主 Key（DeepSeek + Serper）──────────────────────────────
# 客户零配置即用：没填自己 key 时，采集/AI 用平台这把主 key（按会员档限额）。
# 填了自己的 key（BYOK）则用客户自己的、不限额。贵的海关/Apollo 仍走 BYOK。
# SERPER_MASTER_KEYS 可填多个，逗号分隔（自动容错轮换）。不填则不启用平台代付。
DEEPSEEK_MASTER_KEY = os.environ.get("DEEPSEEK_MASTER_KEY", "")
SERPER_MASTER_KEYS  = os.environ.get("SERPER_MASTER_KEYS", "")
