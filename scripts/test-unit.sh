#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

uv run python scripts/run_with_timing.py --label test-unit -- uv run pytest tests/unit -v
