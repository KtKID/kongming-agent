#!/usr/bin/env bash
# scripts/test-with-log.sh — pytest wrapper：同时输出到 stdout + 日志文件
#
# 用法：
#   bash scripts/test-with-log.sh <pytest args>
#
# 例：
#   bash scripts/test-with-log.sh tests/e2e/test_setting_yaml_runtime_safety.py -v
#   bash scripts/test-with-log.sh tests/unit -q
#   bash scripts/test-with-log.sh tests/unit/test_x.py::test_y -v
#
# 行为：
#   1. 在第一个看起来像 "tests/..." 的入参里取 stem 当日志名
#      （没找到时退化为 "pytest"）
#   2. 日志写到 .kongming/test-logs/<stem>-YYYYMMDD-HHMMSS.log
#   3. 同时实时输出到 stdout（让你边跑边看）
#   4. 末尾打印 "📄 日志：<path>"
#   5. 保留 pytest 原始 exit code（pass=0 / fail!=0）
#
# 设计：
#   - .kongming/ 已被 .gitignore 忽略，日志不会污染仓库
#   - 不改 pytest 配置，不影响 pre-commit hook 行为
#   - 不做 retention（v0.2+ 再说）

set -uo pipefail

# 切到项目根，让相对路径稳定
cd "$(dirname "$0")/.."

# 解析第一个看起来像测试文件 / 测试目录的入参，取它的 stem
extract_stem() {
    local arg
    for arg in "$@"; do
        # 跳过 -开头的 flag
        case "$arg" in
            -*) continue ;;
        esac
        # 命中 tests/... 的就用它
        case "$arg" in
            tests/*|tests)
                # 截到第一个 :: 之前（pytest node id 形如 tests/x.py::test_y）
                local path="${arg%%::*}"
                # 去掉 .py 后缀（如有）
                path="${path%.py}"
                # 取最后一个 / 之后的部分
                basename "$path"
                return 0
                ;;
        esac
    done
    echo "pytest"
}

stem=$(extract_stem "$@")
ts=$(date "+%Y%m%d-%H%M%S")
log_dir=".kongming/test-logs"
log_file="${log_dir}/${stem}-${ts}.log"

mkdir -p "$log_dir"

# 写日志头（命令 + 时间），便于事后搜索
{
    echo "=== test-with-log.sh ==="
    echo "command: uv run pytest $*"
    echo "started: $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "cwd: $(pwd)"
    echo "==="
    echo
} > "$log_file"

# 跑 pytest，同时 stdout + 追加到日志
# tee -a：append 到文件（保留头）
# pipefail 已设：管道任一段失败就视为失败
# 这里特别关注 pytest 的 exit code，所以单独捕获
set +e
uv run pytest "$@" 2>&1 | tee -a "$log_file"
exit_code=${PIPESTATUS[0]}
set -e

# 写日志尾
{
    echo
    echo "==="
    echo "ended: $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "exit_code: $exit_code"
} >> "$log_file"

# 提示日志路径
echo "📄 日志：$log_file"

exit "$exit_code"
