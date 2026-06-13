#!/usr/bin/env bash
# scripts/cli-dump.sh
#
# 启动 CLI，同时开启 raw LLM dump（完整 request/response 落到 .kongming/debug/）。
# 用于验证发出去的 payload 是否包含 thinking / reasoning_effort 等字段。
# 默认使用 MiniMax M3 preset。
# 关键流程：进入仓库根目录，开启 KONGMING_TRACE_RAW_LLM，检查用户是否显式传入 --model-preset；
# 未传时追加默认 preset，已传时保留用户参数。
# 关键函数：has_model_preset_arg 判断参数列表是否已有模型 preset。
# 对应 ./start.sh dump。

set -euo pipefail
cd "$(dirname "$0")/.."

# 判断 CLI 参数里是否已有 --model-preset，输入为原始参数列表，输出为 shell 成功/失败状态。
has_model_preset_arg() {
  for arg in "$@"; do
    if [[ "$arg" == "--model-preset" || "$arg" == --model-preset=* ]]; then
      return 0
    fi
  done
  return 1
}

if has_model_preset_arg "$@"; then
  KONGMING_TRACE_RAW_LLM=1 uv run python -m hosts.cli.main --config config/setting.yaml "$@"
else
  KONGMING_TRACE_RAW_LLM=1 uv run python -m hosts.cli.main --config config/setting.yaml --model-preset minimax-m3 "$@"
fi
