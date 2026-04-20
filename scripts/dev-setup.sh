#!/usr/bin/env bash
# scripts/dev-setup.sh
#
# 安装项目依赖，包括 dev extras。
# 假设本机已安装 `uv`（https://github.com/astral-sh/uv）。

set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v uv >/dev/null 2>&1; then
    echo "错误：未找到 uv。请先安装 uv (https://github.com/astral-sh/uv)。" >&2
    exit 1
fi

uv sync --all-extras
