# P0-000 重构基线：常用开发命令。
# 注意：配方使用 TAB 缩进。Windows 无 make 时可直接运行对应的 pytest / ruff 命令。

.PHONY: install-dev lint format test snapshot check

install-dev:
	pip install -r requirements-dev.txt

lint:
	ruff check .
	ruff format --check .

format:
	ruff format .
	ruff check --fix .

test:
	pytest

snapshot:
	UPDATE_SNAPSHOTS=1 pytest tests/test_url_map.py

check: lint test
