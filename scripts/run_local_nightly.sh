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
#   load_e2e_env：只加载本地真实 e2e 环境变量文件中的 KONGMING_* 变量。
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
  local status
  if [[ ! -f "$NIGHTLY_ENV_FILE" ]]; then
    echo "missing $NIGHTLY_ENV_FILE; create it with real model provider settings" >&2
    exit 2
  fi
  status="$(python - "$NIGHTLY_ENV_FILE" <<'PY'
from pathlib import Path
import os
import stat
import sys
path = Path(sys.argv[1])
if path.is_symlink():
    print("symlink")
    sys.exit(1)
st = path.stat()
mode = stat.S_IMODE(st.st_mode)
if st.st_uid != os.getuid():
    print(f"owner:{st.st_uid}")
    sys.exit(1)
if mode != 0o600:
    print(f"mode:{oct(mode)}")
    sys.exit(1)
print("ok")
PY
)"
  if [[ "$status" != "ok" ]]; then
    echo "insecure $NIGHTLY_ENV_FILE ($status); use a regular file owned by you with chmod 600" >&2
    exit 2
  fi
}

# 加载本地真实 e2e 环境变量文件，关键输出是当前 shell 中可见的 KONGMING_* 配置。
load_e2e_env() {
  require_secure_env_file
  while IFS= read -r -d '' key && IFS= read -r -d '' value; do
    printf -v "$key" "%s" "$value"
    export "$key"
  done < <(python - "$NIGHTLY_ENV_FILE" <<'PY'
from pathlib import Path
import ast
import re
import sys

env_path = Path(sys.argv[1])
key_pattern = re.compile(r"^KONGMING_[A-Z0-9_]+$")
allowed_key_pattern = re.compile(
    r"^KONGMING_(MODEL|TOOL|WEB|E2E|SKIP|LLM|PROVIDER)_[A-Z0-9_]+$|^KONGMING_HOME$"
)
value_pattern = re.compile(r"^[A-Za-z0-9._:/@?=&%#,+_-]*$")
forbidden_value_chars = {"$", chr(96), "\r", "\n", "\0"}
for raw_line in env_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key = key.strip()
    if not key_pattern.fullmatch(key):
        continue
    if not allowed_key_pattern.fullmatch(key):
        print(f"unsupported key for {key}", file=sys.stderr)
        sys.exit(2)
    value = value.strip()
    if (value.startswith("'") and value.endswith("'")) or (
        value.startswith('"') and value.endswith('"')
    ):
        value = ast.literal_eval(value)
    if any(char in value for char in forbidden_value_chars):
        print(f"unsafe value for {key}", file=sys.stderr)
        sys.exit(2)
    if not value_pattern.fullmatch(value):
        print(f"unsafe value for {key}", file=sys.stderr)
        sys.exit(2)
    sys.stdout.write(key + "\0" + value + "\0")
PY
  )
}

# 跨平台检查固定测试端口是否空闲，关键输入是 KONGMING_WEB_HOST / KONGMING_WEB_PORT。
require_port_free() {
  if ! python - "$KONGMING_WEB_HOST" "$KONGMING_WEB_PORT" <<'PY'
import socket
import sys
host = sys.argv[1]
port = int(sys.argv[2])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(0.2)
try:
    busy = sock.connect_ex((host, port)) == 0
finally:
    sock.close()
if busy:
    sys.exit(1)
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
  if [[ -f "$lock_file" ]] && [[ "$(cat "$lock_file" 2>/dev/null || true)" == "$$" ]]; then
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
