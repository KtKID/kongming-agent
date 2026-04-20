#!/usr/bin/env bash
# scripts/lint.sh
#
# 运行 ruff check 与 import-linter，覆盖：
#   - 代码风格 / 常见坏味道
#   - 依赖方向（core 不反向依赖 sibling、tools 不直连 safety policy 等）
#
# 对应 make lint。

set -euo pipefail
cd "$(dirname "$0")/.."

uv run ruff check .
uv run lint-imports --config .importlinter
