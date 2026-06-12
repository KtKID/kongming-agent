#!/usr/bin/env bash
# scripts/run_local_nightly.sh
#
# 本地 nightly 测试入口，用于半夜在开发机上跑真实 e2e / integration / smoke。
# 关键执行流程：
#   1. 进入仓库根目录并加载 .env.e2e.local 中的真实模型密钥；
#   2. 固定使用 KONGMING_WEB_PORT=60999 和独立 KONGMING_HOME=.kongming/nightly；
#   3. 启动前检查端口与 nightly lock，避免撞到日常 web 实例；
#   4. 分段运行 integration、e2e、smoke，并把日志写入 .kongming/test-logs/。
#
# 关键函数：
#   load_e2e_env：加载本地真实 e2e 环境变量文件。
#   require_port_free：检查固定测试端口是否空闲。
#   require_clean_nightly_home：检查 nightly home 中是否已有运行锁。
#   run_pytest_group：用 run_with_timing.py 记录一组 pytest 的耗时与日志。

set -euo pipefail

cd "$(dirname "$0")/.."

NIGHTLY_ENV_FILE="${NIGHTLY_ENV_FILE:-.env.e2e.local}"
export KONGMING_WEB_HOST="${KONGMING_WEB_HOST:-127.0.0.1}"
export KONGMING_WEB_PORT="${KONGMING_WEB_PORT:-60999}"
export KONGMING_HOME="${KONGMING_HOME:-$PWD/.kongming/nightly}"
export KONGMING_E2E_REAL_MODEL="${KONGMING_E2E_REAL_MODEL:-1}"

# 加载本地真实 e2e 环境变量文件，关键输出是当前 shell 中可见的 KONGMING_* 配置。
load_e2e_env() {
  if [[ ! -f "$NIGHTLY_ENV_FILE" ]]; then
    echo "missing $NIGHTLY_ENV_FILE; create it with real model provider settings" >&2
    exit 2
  fi
  set -a
  # shellcheck disable=SC1090
  source "$NIGHTLY_ENV_FILE"
  set +a
}

# 检查固定测试端口是否空闲，关键输入是 KONGMING_WEB_PORT。
require_port_free() {
  if lsof -nP -iTCP:"$KONGMING_WEB_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "port $KONGMING_WEB_PORT is already in use; stop that process before nightly" >&2
    lsof -nP -iTCP:"$KONGMING_WEB_PORT" -sTCP:LISTEN >&2 || true
    exit 2
  fi
}

# 检查 nightly 专用 home 是否已有 web app 锁，关键输入是 KONGMING_HOME。
require_clean_nightly_home() {
  local lock_file="$KONGMING_HOME/web/.app.lock"
  if [[ -f "$lock_file" ]]; then
    echo "nightly lock exists: $lock_file" >&2
    echo "remove stale lock after confirming no nightly web process is running" >&2
    exit 2
  fi
}

# 运行一组 pytest，并通过 run_with_timing.py 输出日志文件路径和耗时。
run_pytest_group() {
  local label="$1"
  shift
  uv run python scripts/run_with_timing.py --label "$label" -- \
    uv run pytest "$@" -q --maxfail=3 --durations=20
}

load_e2e_env
require_port_free
require_clean_nightly_home

mkdir -p "$KONGMING_HOME"

echo "local nightly:"
echo "  KONGMING_HOME=$KONGMING_HOME"
echo "  KONGMING_WEB_HOST=$KONGMING_WEB_HOST"
echo "  KONGMING_WEB_PORT=$KONGMING_WEB_PORT"
echo "  KONGMING_E2E_REAL_MODEL=$KONGMING_E2E_REAL_MODEL"

run_pytest_group nightly-integration tests/integration
run_pytest_group nightly-e2e tests/e2e
run_pytest_group nightly-smoke tests/smoke
