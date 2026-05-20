#!/usr/bin/env bash
# create-pr.sh — 逐 commit 重放私有分支白名单变更到公开仓库 dev 分支
#
# 通用脚本，自动检测项目名、remote、分支、GitHub repo。
# 固定 dev 分支：没有 PR 就创建，已有 PR 就追加 commit。
# 保留每个 private commit 的 message / author / date。
#
# 前置条件（由 init-public-pr.sh 一次性配好）：
#   - .publish/include.txt 白名单文件
#   - remote "public" 指向 GitHub
#   - 公开 worktree 在 ../<项目名>-public
#
# 用法：
#   bash bin/create-pr.sh                  # 自动生成 PR 标题
#   bash bin/create-pr.sh "feat: 新功能"    # 自定义 PR 标题
set -euo pipefail

PRIVATE="$(git rev-parse --show-toplevel)"
PROJECT_NAME="$(basename "$PRIVATE")"
PUBLIC="$(cd "$PRIVATE/.." && pwd)/${PROJECT_NAME}-public"
INCLUDE_FILE="$PRIVATE/.publish/include.txt"
PUBLIC_REMOTE="public"
DEV_BRANCH="dev"

# 自动检测：当前分支作为私有分支
PRIVATE_BRANCH="$(git -C "$PRIVATE" rev-parse --abbrev-ref HEAD)"

# 自动检测：从 public remote URL 推导 GitHub owner/repo
GITHUB_REPO=$(git -C "$PRIVATE" remote get-url "$PUBLIC_REMOTE" 2>/dev/null \
    | sed -E 's#.*github\.com[:/]##; s#\.git$##')

[[ -d "$PUBLIC" ]] || { echo "错误：公开 worktree 不存在: $PUBLIC"; exit 1; }
[[ -f "$INCLUDE_FILE" ]] || { echo "错误：缺 .publish/include.txt"; exit 1; }
[[ -n "$GITHUB_REPO" ]] || { echo "错误：无法从 remote '$PUBLIC_REMOTE' 推导 GitHub repo"; exit 1; }

# ── 读白名单 ──

WHITELIST=()
while IFS= read -r line; do
    WHITELIST+=("$line")
done < <(grep -v '^\s*[#]' "$INCLUDE_FILE" | grep -v '^\s*$' | sed 's/[[:space:]]*$//')

[[ ${#WHITELIST[@]} -gt 0 ]] || { echo "错误：白名单为空"; exit 1; }

match_whitelist() {
    local path="$1"
    for entry in "${WHITELIST[@]}"; do
        if [[ "$entry" == */ ]]; then
            [[ "$path" == "$entry"* ]] && return 0
        else
            [[ "$path" == "$entry" ]] && return 0
        fi
    done
    return 1
}

# ── 找上次同步点 ──

cd "$PUBLIC"
git fetch "$PUBLIC_REMOTE" --prune 2>/dev/null || true

if git rev-parse --verify "$PUBLIC_REMOTE/$DEV_BRANCH" >/dev/null 2>&1; then
    git checkout "$DEV_BRANCH" 2>/dev/null || git checkout -b "$DEV_BRANCH" "$PUBLIC_REMOTE/$DEV_BRANCH"
    git reset --hard "$PUBLIC_REMOTE/$DEV_BRANCH" 2>/dev/null || true
    SEARCH_BRANCH="$DEV_BRANCH"
    DEV_EXISTS=true
else
    git checkout main 2>/dev/null
    git pull "$PUBLIC_REMOTE" main --ff-only 2>/dev/null || true
    SEARCH_BRANCH="main"
    DEV_EXISTS=false
fi

LAST_SYNCED=$(git log "$SEARCH_BRANCH" -50 --format='%B' | grep -m1 '^Private-Commit:' | awk '{print $2}' || true)

if [[ -z "$LAST_SYNCED" ]]; then
    echo "错误：找不到 Private-Commit trailer，无法确定上次同步点"
    echo "提示：运行 init-public-pr.sh 初始化，或手动创建锚点 commit"
    exit 1
fi

# ── 收集待重放 commit ──

COMMITS=()
while IFS= read -r hash; do
    [[ -n "$hash" ]] && COMMITS+=("$hash")
done < <(git -C "$PRIVATE" rev-list --reverse "${LAST_SYNCED}..${PRIVATE_BRANCH}")

if [[ ${#COMMITS[@]} -eq 0 ]]; then
    echo "没有新 commit 需要同步"
    exit 0
fi

echo "找到 ${#COMMITS[@]} 个 commit，开始筛选白名单变更..."

# ── 确保在 dev 分支上 ──

if [[ "$DEV_EXISTS" == false ]]; then
    git checkout -b "$DEV_BRANCH"
fi

# ── 逐 commit 重放 ──

SYNCED=0
for hash in "${COMMITS[@]}"; do
    CHANGES=$(git -C "$PRIVATE" diff-tree --no-commit-id --name-status -r --no-renames "$hash" 2>/dev/null)

    TOUCHED=false
    while IFS=$'\t' read -r status filepath; do
        [[ -z "$status" || -z "$filepath" ]] && continue
        match_whitelist "$filepath" || continue
        TOUCHED=true

        case "$status" in
            D)  git rm -f --quiet "$filepath" 2>/dev/null || true ;;
            *)  git checkout "$hash" -- "$filepath" ;;
        esac
    done <<< "$CHANGES"

    $TOUCHED || continue
    git diff --cached --quiet && continue

    ORIG_MSG=$(git -C "$PRIVATE" log -1 --format='%B' "$hash")
    ORIG_AUTHOR=$(git -C "$PRIVATE" log -1 --format='%an <%ae>' "$hash")
    ORIG_DATE=$(git -C "$PRIVATE" log -1 --format='%ai' "$hash")

    git commit --no-verify --author="$ORIG_AUTHOR" --date="$ORIG_DATE" -m "$ORIG_MSG"
    SYNCED=$((SYNCED + 1))
    echo "  ✓ $(git -C "$PRIVATE" log -1 --format='%h %s' "$hash")"
done

if [[ $SYNCED -eq 0 ]]; then
    echo "白名单内无变更，无需同步"
    if [[ "$DEV_EXISTS" == false ]]; then
        git checkout main
        git branch -D "$DEV_BRANCH" 2>/dev/null || true
    fi
    exit 0
fi

# 在最后一个 commit 追加 Private-Commit trailer
PRIVATE_HEAD=$(git -C "$PRIVATE" rev-parse "$PRIVATE_BRANCH")
LAST_MSG=$(git log -1 --format='%B')
git commit --amend --no-verify --no-edit -m "${LAST_MSG%$'\n'}

Private-Commit: $PRIVATE_HEAD"

# ── Push ──

git push "$PUBLIC_REMOTE" "$DEV_BRANCH"

# ── PR：没有就创建，有就只追加 commit ──

EXISTING_PR=$(gh pr list --repo "$GITHUB_REPO" --head "$DEV_BRANCH" --base main --json number -q '.[0].number' 2>/dev/null || true)

if [[ -n "$EXISTING_PR" ]]; then
    echo ""
    echo "已有 PR #${EXISTING_PR}，追加了 $SYNCED 个 commit"
    echo "https://github.com/${GITHUB_REPO}/pull/${EXISTING_PR}"
else
    PR_TITLE="${1:-sync: $(date +%Y-%m-%d) from $(basename "$PRIVATE")}"
    COMMIT_LOG=$(git log main.."$DEV_BRANCH" --oneline)

    PR_URL=$(gh pr create \
        --base main \
        --head "$DEV_BRANCH" \
        --title "$PR_TITLE" \
        --body "$(cat <<EOF
## Commits

${COMMIT_LOG}

---
Private-Commit: \`${PRIVATE_HEAD}\`
EOF
)" \
        --repo "$GITHUB_REPO" 2>&1)

    echo ""
    echo "PR 已创建: $PR_URL"
fi

echo "同步了 $SYNCED 个 commit（共 ${#COMMITS[@]} 个）"
