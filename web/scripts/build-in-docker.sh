#!/usr/bin/env bash
# 在 docker 里跑 web 构建，产物落到 web/dist/
#
# 触发：从 repo 根目录调 `make web-build`，最终走到这里。
# 要求：宿主机有 docker。

set -euo pipefail

WEB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="kongming-web-build:0.1.5"

cd "$WEB_DIR"

mkdir -p dist

echo "==> Building docker image $TAG"
docker build -f Dockerfile.build -t "$TAG" .

echo "==> Running container to copy dist/ → $WEB_DIR/dist/"
# 清空旧产物（保留 dist 目录本身）
find dist -mindepth 1 -delete

docker run --rm \
  -v "$WEB_DIR/dist":/out \
  "$TAG"

echo "==> Done. Output in $WEB_DIR/dist/"
ls -la dist
