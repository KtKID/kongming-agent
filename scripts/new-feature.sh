#!/usr/bin/env bash
# new-feature.sh <topic> — 开新 feature 前一键准备
#
# 解决的问题：避免在 private-main 上裸写、做完才发现没切分支 / 和并行会话(sleep-dev)撞车。
#
# 行为：
#   1. fetch origin，检查本地 private-main 是否落后（落后只提醒，不自动 pull）
#   2. 在 ../kongming-agent-feat-<topic-safe> 建专用 worktree
#   3. 在该 worktree 上建 feat/<topic> 分支（基于本地 private-main）
#   4. uv sync --all-extras（新 worktree 自带独立 .venv，必须装）
#
# 用法：
#   scripts/new-feature.sh web-image-paste
#   scripts/new-feature.sh feat/web-image-paste   # 带不带 feat/ 前缀都行
#
# 配套：开发完成后同步到公开 PR ——
#   python3 scripts/sync-feature.py feat/<topic>   # base 默认已是 private-main
#
# 兼容 macOS 自带 bash 3.2（无 mapfile）。

set -euo pipefail

PRIVATE="/Volumes/machub_app/proj/kongming-agent"
TRUNK="private-main"
WT_PREFIX="/Volumes/machub_app/proj/kongming-agent-feat-"

topic="${1:-}"
if [ -z "$topic" ]; then
  echo "用法: scripts/new-feature.sh <topic>"
  echo "  例: scripts/new-feature.sh web-image-paste"
  exit 1
fi

# 去掉可能带的 feat/ 前缀，统一成 topic
topic="${topic#feat/}"
# worktree 目录后缀：/ 和 _ 都换成 -（和 sync-feature.py 的 feat_safe 规则一致）
safe=$(printf '%s' "$topic" | tr '/_' '--')
branch="feat/$topic"
wt="${WT_PREFIX}${safe}"

echo "📦 feature topic : $topic"
echo "   分支         : $branch"
echo "   worktree     : $wt"
echo "   base         : $TRUNK"
echo

# ─── 1. fetch + 检查 trunk 是否落后 ────────────────────────────────
echo "──────────────────────────────────────────────────"
echo "[1/4] fetch origin + 检查 $TRUNK 是否落后"
git -C "$PRIVATE" fetch origin "$TRUNK" --quiet

behind=$(git -C "$PRIVATE" rev-list --count "$TRUNK..origin/$TRUNK" 2>/dev/null || echo "0")
if [ "$behind" != "0" ]; then
  echo "⚠️  本地 $TRUNK 落后 origin/$TRUNK 共 $behind 个 commit。"
  echo "    新 feature 会基于较旧的本地 $TRUNK，可能与并行会话已推的改动产生冲突。"
  echo "    建议先在 $TRUNK 的 worktree 里 \`git pull\` 拉齐后再跑本脚本。"
  printf "    仍然继续？[y/N] "
  read -r ans
  case "$ans" in
    y | Y) ;;
    *) echo "已中止。"; exit 1 ;;
  esac
else
  echo "✓ 本地 $TRUNK 已是最新"
fi

# ─── 2. 预检：分支 / worktree 是否已存在 ───────────────────────────
echo "──────────────────────────────────────────────────"
echo "[2/4] 预检分支 / worktree 占用"
if git -C "$PRIVATE" rev-parse --verify --quiet "$branch" >/dev/null; then
  echo "❌ 分支 '$branch' 已存在。"
  echo "   换个 topic，或先删除：git -C $PRIVATE branch -D $branch"
  exit 1
fi
if [ -e "$wt" ]; then
  echo "❌ worktree 路径已存在：$wt"
  echo "   先清理：git -C $PRIVATE worktree remove $wt"
  exit 1
fi
echo "✓ 无占用"

# ─── 3. 建专用 worktree + feat 分支 ────────────────────────────────
echo "──────────────────────────────────────────────────"
echo "[3/4] 建 worktree + 分支（基于 $TRUNK）"
git -C "$PRIVATE" worktree add "$wt" -b "$branch" "$TRUNK"
echo "✓ worktree 就绪：$wt（分支 $branch）"

# ─── 4. uv sync（独立 .venv）──────────────────────────────────────
echo "──────────────────────────────────────────────────"
echo "[4/4] uv sync --all-extras（新 worktree 独立环境）"
if command -v uv >/dev/null 2>&1; then
  (cd "$wt" && uv sync --all-extras)
  echo "✓ 依赖就绪"
else
  echo "⚠️  未找到 uv，跳过依赖安装。请手动：cd $wt && uv sync --all-extras"
fi

echo
echo "✅ 准备完成。开始开发："
echo "   cd $wt"
echo
echo "   开发完成后同步到公开 PR："
echo "   python3 scripts/sync-feature.py $branch"
