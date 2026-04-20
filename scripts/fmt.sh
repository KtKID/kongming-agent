#!/usr/bin/env bash
# scripts/fmt.sh
#
# 格式化整个仓库。对应 make fmt。

set -euo pipefail
cd "$(dirname "$0")/.."

uv run ruff format .
