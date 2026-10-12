#!/usr/bin/env bash
# scripts/web.sh
#
# 启动 v0.1.5 web 后端（FastAPI + uvicorn）。
#
# 前置：
#   - 登录密码：仓库默认 config/setting.yaml 的 web.initial_password=admin123，
#     首次启动自动 bcrypt 落盘到 <kongming_home>/web/password.hash；也可用
#     KONGMING_WEB_PASSWORD 覆盖（同样只首次生效，落盘后自动从 env 清掉）
#   - 配置文件 config/setting.yaml 里 web.enabled=true
#   - dev_mode=false（仓库默认）时必须先 make web-build 生成 web/dist，否则启动报
#     RuntimeError；仅 dev_mode=true 时空缺 dist 走占位响应
#
# 注意：本脚本期望 ThreadManager.runtime_factory 由 web 装配的更高层 entrypoint
# 提供（v0.1.5 任务 web-app-shell 仅交付 app/auth/router/ws 层；runtime_factory
# 由后续装配 task 或 host_adapter / cli main 联调时注入）。

set -euo pipefail
cd "$(dirname "$0")/.."

uv run python -m hosts.web.run "$@"
