"""
serve.py — 生产启动入口（用 waitress，单进程多线程）
======================================================
为什么不用 run.py 的 app.run()：那是 Flask 自带开发服务器，不适合生产。
为什么不用 gunicorn 多进程：本系统有「后台自动跟进调度线程」和一些内存状态
（任务进度、限流、数据库连接缓存），多进程会各跑一份、互相看不到。
waitress 单进程多线程刚好：调度只跑一份，内存状态共享，又能并发处理请求。

这里照搬 run.py 的路径/初始化方式（保证拿到的是同一个、完整的 app 实例），
只把启动方式从 app.run() 换成 waitress。平台用环境变量 PORT 指定端口，默认 8080。
"""
import os
import sys
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "web"))
sys.path.insert(0, str(BASE / "core"))
os.chdir(str(BASE))

import admin_db
admin_db.init()

from web.app import app
from waitress import serve

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"外贸雷达 (waitress) 启动于 0.0.0.0:{port}", flush=True)
    serve(app, host="0.0.0.0", port=port, threads=8)
