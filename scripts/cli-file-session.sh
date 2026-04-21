#!/usr/bin/env bash
# scripts/cli-file-session.sh
#
# 启动 CLI，使用 file session backend。
# 用法: bash scripts/cli-file-session.sh [额外的 cli 参数]

set -euo pipefail
cd "$(dirname "$0")/.."

# 临时覆盖 backend=file，不修改原配置文件
uv run python -m cli.main --config config/setting.yaml "$@"
