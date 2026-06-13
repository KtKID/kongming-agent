#!/usr/bin/env bash
# scripts/web-ctl.sh — 薄壳，把 web 进程管理委托给 src/hosts/web/ctl.py。
#
# 历史：v0.1.6 之前是 152 行 bash，因不读 .env / 跨平台靠 || 堆 hack
# 等问题，重构为 Python 实现（src/hosts/web/ctl.py）。bash 仅作 entry shim
# 保持 ./start.sh web ... 用户接口不变。
#
# 用法：
#   bash scripts/web-ctl.sh start     # 启动
#   bash scripts/web-ctl.sh stop      # 关闭
#   bash scripts/web-ctl.sh restart   # 重启
#   bash scripts/web-ctl.sh status    # 状态
#   bash scripts/web-ctl.sh log       # tail 日志
#
# 配置：
#   - 端口 / host / 启用开关：config/setting.yaml 的 web.* 字段
#   - 用 KONGMING_WEB_* env 覆盖（详见 .env.example 的「Web 服务」段）
#   - 首次启动需 KONGMING_WEB_PASSWORD 落盘 .kongming/web/password.hash

set -euo pipefail
cd "$(dirname "$0")/.."

exec uv run python -m hosts.web.ctl "$@"
