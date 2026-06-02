#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

uv run python scripts/run_with_timing.py \
    --label smoke-dump \
    -- env KONGMING_E2E_DEBUG_DUMP_RAW=1 uv run pytest tests/e2e/test_debug_llm_raw.py -v -s "$@"
