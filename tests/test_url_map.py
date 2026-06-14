"""P0-000 URL map 基线快照。

把当前所有路由（URL + 方法 + endpoint）固化成快照。后续重构（如拆分 blueprint）
若意外改变了对外 URL 表面，这条测试会立即失败，从而守住"结构改了、行为没改"。

确有意变更 URL 时，更新快照：
    make snapshot
    # 等价于： UPDATE_SNAPSHOTS=1 pytest tests/test_url_map.py
"""

from __future__ import annotations

import os
from pathlib import Path

SNAPSHOT = Path(__file__).parent / "snapshots" / "url_map.txt"


def current_rules(app) -> list[str]:
    """返回排序后的 '规则 [方法] -> endpoint' 列表（忽略 HEAD/OPTIONS）。"""
    rules = []
    for rule in app.url_map.iter_rules():
        methods = ",".join(sorted(m for m in rule.methods if m not in {"HEAD", "OPTIONS"}))
        rules.append(f"{rule.rule} [{methods}] -> {rule.endpoint}")
    return sorted(rules)


def test_url_map_matches_snapshot(flask_app):
    current = current_rules(flask_app)

    # 显式开启时重新生成基线（reuse 同一夹具环境），否则只做比对。
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text("\n".join(current) + "\n", encoding="utf-8")

    assert SNAPSHOT.exists(), "URL map 快照缺失：运行 `make snapshot` 生成"
    expected = SNAPSHOT.read_text(encoding="utf-8").splitlines()
    assert current == expected, (
        "URL 表与基线快照不一致。若为有意变更，运行 `make snapshot` 更新；"
        "否则说明重构意外改动了对外路由。"
    )
