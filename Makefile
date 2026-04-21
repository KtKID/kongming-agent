# kongming-agent 统一命令入口
#
# 所有命令都走 uv 运行，避免开发者各自发明虚拟环境 / 激活流程。
# 真正的执行脚本放在 scripts/，这里只是对外的简短别名。

SHELL := /usr/bin/env bash

.PHONY: help install install-hooks fmt fmt-check lint typecheck \
        precommit test test-unit test-e2e smoke cli clean

help:
	@echo "kongming-agent Makefile"
	@echo ""
	@echo "  make install       安装依赖（uv sync --all-extras）"
	@echo "  make install-hooks 启用 pre-commit hook（commit 前自动跑软编译）"
	@echo "  make fmt           ruff format ."
	@echo "  make fmt-check     ruff format --check ."
	@echo "  make lint          ruff check . + import-linter"
	@echo "  make typecheck     mypy 所有模块"
	@echo "  make precommit     手动跑一次 pre-commit 全仓扫描"
	@echo "  make test-unit     pytest tests/unit -v"
	@echo "  make test-e2e      pytest tests/e2e -v"
	@echo "  make test          test-unit + test-e2e"
	@echo "  make smoke         最小启动 smoke test"
	@echo "  make cli           启动 CLI（本地模型基线配置）"
	@echo "  make clean         清理缓存产物"

install:
	bash scripts/dev-setup.sh

install-hooks:
	uv run pre-commit install

precommit:
	uv run pre-commit run --all-files

fmt:
	bash scripts/fmt.sh

fmt-check:
	uv run ruff format --check .

lint:
	bash scripts/lint.sh

typecheck:
	bash scripts/typecheck.sh

test-unit:
	bash scripts/test-unit.sh

test-e2e:
	bash scripts/test-e2e.sh

test: test-unit test-e2e

smoke:
	bash scripts/smoke.sh

cli:
	bash scripts/cli.sh

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov coverage.xml
	rm -rf build dist *.egg-info
	find . -type d -name __pycache__ -not -path "./other/*" -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -not -path "./other/*" -not -path "./.venv/*" -delete 2>/dev/null || true
