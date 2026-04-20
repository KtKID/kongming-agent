#!/usr/bin/env bash
# scripts/test-unit.sh
#
# 运行单元测试。单元测试只验证模块内部职责与边界，不依赖外部模型服务。
# 对应 make test-unit。

set -euo pipefail
cd "$(dirname "$0")/.."

uv run pytest tests/unit -v
