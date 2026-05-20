# kongming-agent 统一命令入口
#
# 所有命令都走 uv 运行，避免开发者各自发明虚拟环境 / 激活流程。
# 真正的执行脚本放在 scripts/，这里只是对外的简短别名。

SHELL := /usr/bin/env bash

.PHONY: help install install-hooks fmt fmt-check lint typecheck \
        precommit test test-unit test-e2e smoke cli clean \
        web-build web-dev web-test web

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
	uv run pre-commit install --hook-type pre-commit --hook-type pre-push

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

# ---------- web frontend (v0.1.5) ----------
# web-build：在 docker 里跑 npm ci + npm run build；产物在 web/dist/
# web-dev   ：本地 vite dev server（不带后端，需要后端时另外跑 `make web` 之类）
# web-test  ：跑前端 vitest 单测

web-build:
	bash web/scripts/build-in-docker.sh

web-dev:
	cd web && npm run dev

web-test:
	cd web && npm run test:unit

# web：启动 uvicorn web 后端（v0.1.5 web-app-shell）。
# 前置：
#   - export KONGMING_WEB_PASSWORD=<your-pwd>（首次必填）
#   - 配置文件 cfg.web.enabled=true / dev_mode 视情况
#   - 前端可选：先 make web-build 让 web/dist 存在
# 默认 host=0.0.0.0 port=8080；按 cfg.web 实际值覆盖
web:
	bash scripts/web.sh
