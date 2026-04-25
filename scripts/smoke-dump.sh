#!/usr/bin/env bash
# scripts/smoke-dump.sh
#
# 跑 e2e dump 测试：真实发一次 LLM 请求，把完整 request/response 落盘到
# .kongming/debug/raw-llm-*.json，同时输出 httpx DEBUG 日志。
# 对应 ./start.sh smoke-dump。
#
# 验证 reasoning_effort 是否真的出现在 payload 里：
#   jq '.request.payload | {model, thinking, reasoning_effort}' \
#     .kongming/debug/$(ls -t .kongming/debug/raw-llm-*.json | head -1)

set -euo pipefail
cd "$(dirname "$0")/.."

KONGMING_E2E_DEBUG_DUMP_RAW=1 uv run pytest tests/e2e/test_debug_llm_raw.py -v -s "$@"
