#!/usr/bin/env bash
# scripts/typecheck.sh
#
# 运行 mypy，覆盖所有一级模块。core/ 开启严格模式，其余模块渐进式收严。
# 对应 make typecheck。

set -euo pipefail
cd "$(dirname "$0")/.."

uv run mypy src/core src/tools src/context src/executors src/host src/cli src/safety src/observability
