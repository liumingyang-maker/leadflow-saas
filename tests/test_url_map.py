"""P0-000 URL map baseline snapshot.

Update intentionally with:
    make snapshot
    # equivalent: UPDATE_SNAPSHOTS=1 pytest tests/test_url_map.py
"""

from __future__ import annotations

import os
from pathlib import Path

SNAPSHOT = Path(__file__).parent / "snapshots" / "url_map.txt"
AUTO_METHODS = frozenset({"HEAD", "OPTIONS"})


def _sort_key(line: str) -> tuple[str, str, str]:
    parts = line.split(" | ", maxsplit=2)
    if len(parts) != 3:
        return ("", line, "")
    methods, rule, endpoint = parts
    return (rule, methods, endpoint)


def _sorted_lines(lines: list[str]) -> list[str]:
    return sorted(lines, key=_sort_key)


def current_rules(app) -> list[str]:
    """Return stable 'methods | URL rule | endpoint' snapshot rows."""
    rules: list[str] = []
    for rule in app.url_map.iter_rules():
        methods = ",".join(sorted(m for m in rule.methods if m not in AUTO_METHODS))
        rules.append(f"{methods} | {rule.rule} | {rule.endpoint}")
    return _sorted_lines(rules)


def test_url_map_matches_snapshot(flask_app):
    current = current_rules(flask_app)

    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text("\n".join(current) + "\n", encoding="utf-8")

    assert SNAPSHOT.exists(), "URL map snapshot is missing: run `make snapshot`"
    expected = _sorted_lines(SNAPSHOT.read_text(encoding="utf-8").splitlines())
    assert current == expected, (
        "URL map does not match the baseline snapshot. If this route change is "
        "intentional, run `make snapshot`; otherwise a refactor changed public URLs."
    )
