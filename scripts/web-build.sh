#!/usr/bin/env bash
# scripts/web-build.sh — 本地前端构建入口
set -euo pipefail
cd "$(dirname "$0")/../web"
npm run build "$@"
