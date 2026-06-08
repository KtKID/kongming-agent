#!/usr/bin/env bash
# scripts/typecheck.sh
#
# 运行 mypy，覆盖所有一级模块。core/ 开启严格模式，其余模块渐进式收严。
# 对应 make typecheck。

set -euo pipefail
cd "$(dirname "$0")/.."

uv run mypy \
  src/core \
  src/tools \
  src/sessions \
  src/prompting \
  src/infrastructure \
  src/application \
  src/runtime_assembly \
  src/hosts \
  src/safety \
  src/memory \
  src/evolution \
  src/scheduler \
  src/network
