#!/usr/bin/env bash
# scripts/cli.sh
#
# 启动 CLI。默认使用本地模型基线配置，便于接本地 OpenAI-compatible 服务。
# 对应 make cli。

set -euo pipefail
cd "$(dirname "$0")/.."

uv run python -m cli.main --config config/setting.yaml "$@"
