#!/usr/bin/env bash
# scripts/smoke.sh
#
# 最小启动 smoke：用默认配置跑 CLI 的 --smoke 模式验证装配和 provider；
# 再跑 --workflow-smoke 验证 run_agent_workflow 工具入口、审批链和 map_reduce planner。
# 对应 make smoke。
#
# 注意：--smoke 会发起一次真实模型请求；--workflow-smoke 不发起模型请求。

set -euo pipefail
cd "$(dirname "$0")/.."

KONGMING_SESSION_BACKEND=memory KONGMING_APPROVAL_MODE=auto_allow uv run python -m cli.main --model-preset minimax-m3 --workflow-smoke
KONGMING_SESSION_BACKEND=memory uv run python -m cli.main --model-preset minimax-m3 --smoke
