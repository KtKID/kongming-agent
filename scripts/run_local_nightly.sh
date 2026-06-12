#!/usr/bin/env bash
# scripts/run_local_nightly.sh
#
# 本地 nightly 测试入口，用于半夜在开发机上跑真实 e2e / integration / smoke。
# 关键执行流程：
#   1. 进入仓库根目录并加载 .env.e2e.local 中的真实模型密钥；
#   2. 固定使用 KONGMING_WEB_PORT=60999 和独立 KONGMING_HOME=.kongming/nightly；
#   3. 启动前检查端口、env 权限与 nightly lock，避免撞到日常 web 实例；
#   4. 分段运行 integration、e2e、smoke，并把日志写入 .kongming/test-logs/。
#
# 关键函数：
#   load_e2e_env：加载本地真实 e2e 环境变量文件。
#   require_secure_env_file：检查 env 文件权限。
#   require_port_free：跨平台检查固定测试端口是否空闲。
#   require_clean_nightly_home：检查 nightly home 中是否已有运行锁。
#   cleanup_nightly：退出时清理当前 nightly 产生的锁文件。
#   run_pytest_group：用 run_with_timing.py 记录一组 pytest 的耗时与日志。

set -Eeuo pipefail

cd "$(dirname "$0")/.."

NIGHTLY_ENV_FILE="${NIGHTLY_ENV_FILE:-.env.e2e.local}"
export KONGMING_WEB_HOST="${KONGMING_WEB_HOST:-127.0.0.1}"
export KONGMING_WEB_PORT="${KONGMING_WEB_PORT:-60999}"
export KONGMING_HOME="${KONGMING_HOME:-$PWD/.kongming/nightly}"
export KONGMING_E2E_REAL_MODEL="${KONGMING_E2E_REAL_MODEL:-1}"

# 检查 env 文件权限，关键输入是 NIGHTLY_ENV_FILE，输出是安全或失败。
require_secure_env_file() {
  local mode
  if [[ ! -f "$NIGHTLY_ENV_FILE" ]]; then
    echo "missing $NIGHTLY_ENV_FILE; create it with real model provider settings" >&2
    exit 2
  fi
  mode="$(python - "$NIGHTLY_ENV_FILE" <<'PY'
from pathlib import Path
import stat
import sys
mode = stat.S_IMODE(Path(sys.argv[1]).stat().st_mode)
print(oct(mode))
PY
)"
  if [[ "$mode" != "0o600" ]]; then
    echo "insecure permissions on $NIGHTLY_ENV_FILE: $mode; run chmod 600 $NIGHTLY_ENV_FILE" >&2
    exit 2
  fi
}

# 加载本地真实 e2e 环境变量文件，关键输出是当前 shell 中可见的 KONGMING_* 配置。
load_e2e_env() {
  require_secure_env_file
  set -a
  # shellcheck disable=SC1090
  source "$NIGHTLY_ENV_FILE"
  set +a
}

# 跨平台检查固定测试端口是否空闲，关键输入是 KONGMING_WEB_HOST / KONGMING_WEB_PORT。
require_port_free() {
  if ! python - "$KONGMING_WEB_HOST" "$KONGMING_WEB_PORT" <<'PY'
import socket
import sys
host = sys.argv[1]
port = int(sys.argv[2])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind((host, port))
except OSError:
    sys.exit(1)
finally:
    sock.close()
PY
  then
    echo "port $KONGMING_WEB_HOST:$KONGMING_WEB_PORT is already in use; stop that process before nightly" >&2
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

# 退出时清理当前 nightly 可能留下的本地锁文件，关键输入是 KONGMING_HOME。
cleanup_nightly() {
  local lock_file="$KONGMING_HOME/web/.app.lock"
  if [[ -f "$lock_file" ]]; then
    rm -f "$lock_file"
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
trap cleanup_nightly EXIT

echo "local nightly:"
echo "  KONGMING_HOME=$KONGMING_HOME"
echo "  KONGMING_WEB_HOST=$KONGMING_WEB_HOST"
echo "  KONGMING_WEB_PORT=$KONGMING_WEB_PORT"
echo "  KONGMING_E2E_REAL_MODEL=$KONGMING_E2E_REAL_MODEL"

run_pytest_group nightly-integration tests/integration
run_pytest_group nightly-e2e tests/e2e
run_pytest_group nightly-smoke tests/smoke
