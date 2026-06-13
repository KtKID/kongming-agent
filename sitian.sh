#!/usr/bin/env bash
# =============================================================================
# 司天扫描封装脚本（Mac / Linux）
# 等价的 Windows PowerShell 版本：./SiTianRun.ps1
#
# 把以下三个命令收口成一个入口：
#   uv run kongming-sitian run-once  扫一次
#   uv run kongming-sitian state     读当前状态
#   uv run kongming-sitian loop      持续循环
#
# 用法：
#   ./sitian.sh scan       一次扫描 + 打印 state + 打印 summary（默认）
#   ./sitian.sh state      只读当前状态（不扫）
#   ./sitian.sh loop       持续循环扫描，Ctrl+C 退出
#   ./sitian.sh summary    只 cat latest_summary.md
#   ./sitian.sh clean      删除产物目录（小心，会清空 ROOT_DIR）
#   ./sitian.sh help       本帮助
#
# 通用参数（可出现在任意位置）：
#   --root  DIR    产物目录，默认 <kongming_home>/sitian
#   --config FILE  配置文件，默认 config/sitian.local.yaml
#
# 优先级：CLI 参数 > 环境变量（SITIAN_ROOT / SITIAN_CONFIG）> 默认值
#
# 例子：
#   ./sitian.sh                                    扫一次（默认 scan）
#   ./sitian.sh scan --root ~/.SiTian              产物落到家目录
#   ./sitian.sh state --root ~/.SiTian             看状态
#   ./sitian.sh loop --root ~/.SiTian              持续循环
#   ./sitian.sh scan --config config/my.yaml       换配置文件
# =============================================================================

set -euo pipefail

# 切到脚本所在目录，让相对路径稳定
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---- 解析 CLI 参数（--root / --config 可出现在任意位置）----
_cli_root=""
_cli_config=""
_positional=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --root)   _cli_root="$2";   shift 2 ;;
        --config) _cli_config="$2"; shift 2 ;;
        *)        _positional+=("$1"); shift ;;
    esac
done
set -- "${_positional[@]+"${_positional[@]}"}"   # 还原位置参数

# ---- 配置项：CLI 参数 > 环境变量 > 默认值 ----
CONFIG_PATH="${_cli_config:-${SITIAN_CONFIG:-config/sitian.local.yaml}}"
ROOT_DIR="${_cli_root:-${SITIAN_ROOT:-}}"

# 把 ~ 展成绝对路径（避免 yaml/CLI 拿到字面 ~ 解析失败）
if [ -n "$ROOT_DIR" ]; then
    ROOT_DIR="${ROOT_DIR/#\~/$HOME}"
fi
CONFIG_PATH="${CONFIG_PATH/#\~/$HOME}"

# ROOT_DIR 为空时不传 --root-dir，让 Python 代码走默认（<kongming_home>/sitian/）
_root_args=()
if [ -n "$ROOT_DIR" ]; then
    _root_args=(--root-dir "$ROOT_DIR")
fi

# ---- 帮助 ----
print_help() {
    cat <<'EOF'
司天扫描封装脚本

用法:
  ./sitian.sh [子命令] [--root DIR] [--config FILE]

子命令:
  scan      一次扫描 + 打印 state + 打印 summary（默认）
  state     只读当前状态
  loop      持续循环扫描，Ctrl+C 退出
  summary   只打印 latest_summary.md
  clean     删除产物目录
  help      显示本帮助

通用参数:
  --root DIR      产物目录，默认 <kongming_home>/sitian
  --config FILE   配置文件，默认 config/sitian.local.yaml

优先级: CLI 参数 > 环境变量 (SITIAN_ROOT / SITIAN_CONFIG) > 默认值

例子:
  ./sitian.sh                                    # 扫一次
  ./sitian.sh scan --root ~/.SiTian              # 产物落家目录
  ./sitian.sh state --root ~/.SiTian             # 看状态
  ./sitian.sh loop --root ~/.SiTian              # 持续循环
  ./sitian.sh scan --config config/my.yaml       # 换配置文件
EOF
}

# ---- 子命令前置：检查配置文件存在 ----
require_config() {
    if [ ! -f "$CONFIG_PATH" ]; then
        echo "✗ 配置文件不存在：$CONFIG_PATH" >&2
        echo "  可用 SITIAN_CONFIG 环境变量覆盖路径。" >&2
        exit 1
    fi
}

# ---- 打印 summary（通过 state 命令拿 latestSummary，由 CLI 处理 subdir 路径）----
show_summary() {
    echo
    echo "===== SiTian Summary ====="
    # state 命令吐 JSON 含 latestSummary 字段；如果产物不存在或读不到，
    # latestSummary 为 null，下面的 fallback 文案会兜底
    local state_json
    if [ -f "$CONFIG_PATH" ]; then
        state_json=$(uv run kongming-sitian state \
            --config "$CONFIG_PATH" \
            "${_root_args[@]}" 2>/dev/null || true)
    else
        state_json=$(uv run kongming-sitian state \
            "${_root_args[@]}" 2>/dev/null || true)
    fi
    # 用 python 提 latestSummary（不依赖 jq；通过 stdin 传 JSON 避免转义）
    local summary
    summary=$(printf '%s' "$state_json" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    s = data.get('latestSummary')
    print(s if s else '')
except Exception:
    pass
" 2>/dev/null)
    if [ -n "$summary" ]; then
        echo "$summary"
    else
        echo "(尚未生成 latest_summary.md，先跑一次 scan)" >&2
    fi
}

# ---- 主分派 ----
ACTION="${1:-scan}"

case "$ACTION" in
    scan|run-once)
        require_config
        echo "▶ 跑一次扫描"
        echo "  config = $CONFIG_PATH"
        echo "  root   = ${ROOT_DIR:-<default: kongming_home/sitian/>}"
        uv run kongming-sitian run-once \
            --config "$CONFIG_PATH" \
            "${_root_args[@]}"
        echo
        echo "▶ 当前状态"
        # state 也传 --config，让 sitian.output_subdir 自动生效
        uv run kongming-sitian state \
            --config "$CONFIG_PATH" \
            "${_root_args[@]}"
        show_summary
        ;;

    state)
        # 优先传 --config（如果配置文件存在），让 output_subdir 生效
        if [ -f "$CONFIG_PATH" ]; then
            uv run kongming-sitian state --config "$CONFIG_PATH" "${_root_args[@]}"
        else
            uv run kongming-sitian state "${_root_args[@]}"
        fi
        ;;

    loop)
        require_config
        echo "▶ 进入循环模式（Ctrl+C 退出）"
        echo "  config = $CONFIG_PATH"
        echo "  root   = $ROOT_DIR"
        uv run kongming-sitian loop \
            --config "$CONFIG_PATH" \
            "${_root_args[@]}"
        ;;

    summary)
        show_summary
        ;;

    clean)
        # 防误删：拒绝清根目录 / 家目录这类敏感路径
        case "$ROOT_DIR" in
            /|/Users|/Users/*|"$HOME")
                echo "✗ 拒绝清理敏感目录：$ROOT_DIR" >&2
                exit 1
                ;;
        esac
        if [ -d "$ROOT_DIR" ]; then
            echo "▶ 清理 $ROOT_DIR"
            rm -rf "$ROOT_DIR"
            echo "✓ 已清理"
        else
            echo "(目录不存在，无需清理：$ROOT_DIR)"
        fi
        ;;

    help|--help|-h)
        print_help
        ;;

    *)
        echo "✗ 未知子命令：$ACTION" >&2
        echo
        print_help
        exit 1
        ;;
esac
