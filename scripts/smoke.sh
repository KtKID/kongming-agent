#!/usr/bin/env bash
# scripts/smoke.sh
#
# 最小启动 smoke：用默认配置跑 CLI 的 --smoke 模式，验证装配能通过。
# 对应 make smoke。
#
# 注意：smoke 模式应当尽量不依赖真实模型服务，实现由 cli/main.py 负责。

set -euo pipefail
cd "$(dirname "$0")/.."

uv run python -m cli.main --config config/default.yaml --smoke
