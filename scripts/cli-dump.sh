#!/usr/bin/env bash
# scripts/cli-dump.sh
#
# 启动 CLI，同时开启 raw LLM dump（完整 request/response 落到 .kongming/debug/）。
# 用于验证发出去的 payload 是否包含 thinking / reasoning_effort 等字段。
# 对应 ./start.sh dump。

set -euo pipefail
cd "$(dirname "$0")/.."

KONGMING_TRACE_RAW_LLM=1 uv run python -m cli.main --config config/setting.yaml "$@"
