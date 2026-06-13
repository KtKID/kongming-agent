#!/usr/bin/env bash
# scripts/cli.sh
#
# 启动 CLI。默认使用 MiniMax M3 preset。
# 关键流程：进入仓库根目录，检查用户是否显式传入 --model-preset；
# 未传时追加默认 preset，已传时保留用户参数。
# 关键函数：has_model_preset_arg 判断参数列表是否已有模型 preset。
# 对应 make cli。

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
  uv run python -m hosts.cli.main --config config/setting.yaml "$@"
else
  uv run python -m hosts.cli.main --config config/setting.yaml --model-preset minimax-m3 "$@"
fi
