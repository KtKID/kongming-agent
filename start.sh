#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$ROOT_DIR/scripts"

print_help() {
    cat <<'EOF'
kongming-agent unified entry script

Usage:
  ./start.sh <command> [args...]
  ./start.cmd <command> [args...]   # Windows CMD / PowerShell
  ./start.ps1 <command> [args...]   # Windows PowerShell
  ./start.sh --debug [args...]
  ./start.sh help

Commands:
  install
    安装项目依赖。
    Example: ./start.sh install

  cli
    启动默认 CLI，额外参数会透传给 scripts/cli.sh。
    Example: ./start.sh cli --verbose
    Example: ./start.sh cli --debug
    Example: ./start.sh cli --list-sessions
    Example: ./start.sh cli --resume-last
    Example: ./start.sh cli --instructions-file prompts/role.md --instructions-file prompts/tools.md

  --debug
    启动默认 CLI，并保存每轮 system prompt 和完整 history 到 .kongming/debug/。
    Example: ./start.sh --debug --session-id demo

  -w <path> | -ws <path>
    启动 CLI 并切到指定工作目录（透传成 cli.sh --workdir <path>）。
    LLM 看到的 cwd 即此路径。后续参数继续透传给 cli.sh。
    Example: ./start.sh -w ~/Notes
    Example: ./start.sh -ws ~/Notes --verbose

  web start | web stop | web restart | web status | web log | web build
    Web 服务管理（透传给 scripts/web-ctl.sh）。
    Example: ./start.sh web build       # 仅编译前端
    Example: ./start.sh web start --build  # 编译前端 + 启动 web server
    Example: ./start.sh web start       # 启动 web server（首次需设 PASSWORD）
    Example: ./start.sh web stop        # 关闭
    Example: ./start.sh web restart     # 重启
    Example: ./start.sh web status      # 查看运行状态
    Example: ./start.sh web log         # 查看运行日志

  cli-file-session
    启动 file session 版本 CLI，额外参数会透传给 scripts/cli-file-session.sh。
    Example: ./start.sh cli-file-session --session-id demo

  smoke
    运行最小 smoke 检查。
    Example: ./start.sh smoke

  smoke-dump
    跑 e2e dump 测试（tests/e2e/test_debug_llm_raw.py），真实发请求并落盘完整 payload。
    用于验证 reasoning_effort / thinking 字段是否出现在发出去的 request 里。
    Example: ./start.sh smoke-dump

  dump
    启动 CLI 并开启 raw LLM dump（每次请求落盘到 .kongming/debug/）。
    Example: ./start.sh dump --reasoning-effort high

  fmt
    格式化整个仓库。
    Example: ./start.sh fmt

  lint
    运行 Ruff 和 import-linter。
    Example: ./start.sh lint

  typecheck
    运行 mypy 类型检查。
    Example: ./start.sh typecheck

  test-unit
    运行单元测试。
    Example: ./start.sh test-unit

  test-e2e
    运行端到端测试。
    Example: ./start.sh test-e2e

  help
    显示帮助信息。
    Example: ./start.sh help

Notes:
  - `cli` 和 `cli-file-session` 会把后续参数完整透传给底层脚本。
  - `web` 后台启动 Web 聊天服务（FastAPI + uvicorn），有独立 runtime，不需要 CLI。
  - 所有命令都在仓库根目录下执行，避免工作目录漂移。
EOF
}

run_script() {
    local script_name="$1"
    shift

    local script_path="$SCRIPTS_DIR/$script_name"
    if [[ ! -f "$script_path" ]]; then
        echo "错误：脚本不存在：$script_path" >&2
        exit 1
    fi

    bash "$script_path" "$@"
}

main() {
    local command="${1:-help}"

    case "$command" in
        --debug)
            shift || true
            run_script "cli.sh" --debug "$@"
            ;;
        -w|-ws)
            shift || true
            if [[ $# -lt 1 ]]; then
                echo "错误：$command 需要一个路径参数（如 ./start.sh -w ~/Notes）" >&2
                exit 2
            fi
            local workdir="$1"
            shift || true
            run_script "cli.sh" --workdir "$workdir" "$@"
            ;;
        help|-h|--help)
            print_help
            ;;
        install)
            shift || true
            run_script "dev-setup.sh" "$@"
            ;;
        cli)
            shift || true
            run_script "cli.sh" "$@"
            ;;
        web)
            shift || true
            if [[ $# -lt 1 ]]; then
                echo "错误：web 需要子命令（start / stop / restart / status / log / build）" >&2
                echo "    例：./start.sh web start" >&2
                exit 2
            fi
            local web_sub="$1"
            if [[ "$web_sub" == "build" ]]; then
                shift || true
                echo "Building web frontend..."
                (cd "$ROOT_DIR/web" && npm run build)
            elif [[ "$web_sub" == "start" && " $* " == *" --build "* ]]; then
                # ./start.sh web start --build → 先编译再启动
                echo "Building web frontend..."
                (cd "$ROOT_DIR/web" && npm run build)
                # 去掉 --build 参数再传给 web-ctl
                local args=()
                for arg in "$@"; do
                    [[ "$arg" != "--build" ]] && args+=("$arg")
                done
                run_script "web-ctl.sh" "${args[@]}"
            else
                run_script "web-ctl.sh" "$@"
            fi
            ;;
        cli-file-session)
            shift || true
            run_script "cli-file-session.sh" "$@"
            ;;
        smoke)
            shift || true
            run_script "smoke.sh" "$@"
            ;;
        smoke-dump)
            shift || true
            run_script "smoke-dump.sh" "$@"
            ;;
        dump)
            shift || true
            run_script "cli-dump.sh" "$@"
            ;;
        fmt)
            shift || true
            run_script "fmt.sh" "$@"
            ;;
        lint)
            shift || true
            run_script "lint.sh" "$@"
            ;;
        typecheck)
            shift || true
            run_script "typecheck.sh" "$@"
            ;;
        test-unit)
            shift || true
            run_script "test-unit.sh" "$@"
            ;;
        test-e2e)
            shift || true
            run_script "test-e2e.sh" "$@"
            ;;
        *)
            echo "错误：未识别的命令：$command" >&2
            echo "" >&2
            print_help >&2
            exit 2
            ;;
    esac
}

main "$@"
