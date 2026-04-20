#!/usr/bin/env bash
# scripts/test-e2e.sh
#
# 运行 e2e 测试。e2e 验证主调用链闭环，部分用例需要本地 OpenAI-compatible
# 模型服务（参见 config/local-model.yaml）。
# 对应 make test-e2e。

set -euo pipefail
cd "$(dirname "$0")/.."

uv run pytest tests/e2e -v
