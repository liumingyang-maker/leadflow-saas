"""
run.py — LeadFlow SaaS 启动入口
"""
import sys
import os
import logging
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "web"))
sys.path.insert(0, str(BASE / "core"))   # 核心模块
os.chdir(str(BASE))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(str(BASE / "saas.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)

import admin_db
admin_db.init()
logging.info("数据库初始化完成")

from web.app import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    logging.info(f"LeadFlow SaaS 启动: http://127.0.0.1:{port}")
    logging.info(f"管理后台: http://127.0.0.1:{port}/admin")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
