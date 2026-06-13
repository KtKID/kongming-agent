#!/usr/bin/env bash
#
# ============================================================================
# ⛔️ DEPRECATED — 不要再用！
# ----------------------------------------------------------------------------
# 替代方案：scripts/sync-group.py（按 .publish/groups.yaml 手工分组）
#
# 废弃原因：C 方案"按 commit message scope 自动路由"在跨模块依赖密集的代码库
#           下不可行——同一 feature 的 commit 会被拆到不同 dev-* 分支，
#           每个分支单独跑 CI 都因为缺别的 scope 引入的代码而 mypy 挂。
#
# 保留这份做历史参考；如果脚本被误调用会污染 git 分支，请改用 sync-group.py。
# ============================================================================
#
# create-pr.sh — 按 conventional commit scope 路由到多个公开 PR（C 方案）

echo "⛔️ DEPRECATED — 请改用 scripts/sync-group.py" >&2
exit 1
# 原脚本内容保留在下面（如果你确实想看历史实现，去掉上面 exit）：
#
# 工作原理：
#   - 私有 commit message 形如 ``feat(<scope>): xxx`` / ``fix(<scope>): xxx``
#   - 脚本解析 scope，把 commit 分发到 ``dev-<scope>`` 分支
#   - 每个 ``dev-<scope>`` 对应一个公开 PR（同 feature 同 PR，不同 feature 分 PR）
#
# 行为规则：
#   - scope 不能解析（裸 ``feat: xxx`` / ``Merge ...`` 等）→ 归到 dev-misc
#   - 同 scope 已有 open PR → 追加 commit；没有 → 创建新 PR
#   - 保留每个 commit 的 message / author / date
#   - 最后一个 PR 的最后一个 commit 加 Private-Commit trailer（推进同步锚点）
#
# 前置条件：
#   - .publish/include.txt 白名单文件
#   - remote "public" 指向 GitHub
#   - 公开 worktree 在 ../<项目名>-public
#
# 用法：
#   bash scripts/create-pr.sh              # 真跑：分组、push、开 PR
#   bash scripts/create-pr.sh --dry-run    # 模拟：只打印分组，不动远端
#
# 注意：不开 ``set -u``（macOS bash 3.2 + 空数组遍历不兼容）
set -eo pipefail

# ─── 参数解析 ───────────────────────────────────────────────────────

DRY_RUN=false
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        *) ;;
    esac
done

# ─── 路径/分支自动检测 ──────────────────────────────────────────────

PRIVATE="$(git rev-parse --show-toplevel)"
PROJECT_NAME="$(basename "$PRIVATE")"
PUBLIC="$(cd "$PRIVATE/.." && pwd)/${PROJECT_NAME}-public"
INCLUDE_FILE="$PRIVATE/.publish/include.txt"
PUBLIC_REMOTE="public"
MISC_SCOPE="misc"  # scope 解析失败时的兜底名

PRIVATE_BRANCH="$(git -C "$PRIVATE" rev-parse --abbrev-ref HEAD)"

GITHUB_REPO=$(git -C "$PRIVATE" remote get-url "$PUBLIC_REMOTE" 2>/dev/null \
    | sed -E 's#.*github\.com[:/]##; s#\.git$##')

[[ -d "$PUBLIC" ]] || { echo "错误：公开 worktree 不存在: $PUBLIC"; exit 1; }
[[ -f "$INCLUDE_FILE" ]] || { echo "错误：缺 .publish/include.txt"; exit 1; }
[[ -n "$GITHUB_REPO" ]] || { echo "错误：无法从 remote '$PUBLIC_REMOTE' 推导 GitHub repo"; exit 1; }

$DRY_RUN && echo "🔍 DRY-RUN 模式：只打印分组计划，不动远端"
echo "公开 worktree: $PUBLIC"
echo "私有分支: $PRIVATE_BRANCH"
echo "GitHub 仓库: $GITHUB_REPO"
echo ""

# ─── 读白名单 ───────────────────────────────────────────────────────

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

# ─── 找上次同步点 ───────────────────────────────────────────────────

cd "$PUBLIC"
git fetch "$PUBLIC_REMOTE" --prune 2>/dev/null || true

# 在 main 上找 Private-Commit trailer（PR merge 后 main 会有；首次同步前需要锚点）
git checkout main 2>/dev/null
git pull "$PUBLIC_REMOTE" main --ff-only 2>/dev/null || true

LAST_SYNCED=$(git log main -100 --format='%B' | grep -m1 '^Private-Commit:' | awk '{print $2}' || true)

if [[ -z "$LAST_SYNCED" ]]; then
    # 兜底：从 dev-* 远端分支找 trailer（PR 没 merge 时）
    for ref in $(git for-each-ref --format='%(refname:short)' "refs/remotes/$PUBLIC_REMOTE/dev*"); do
        LAST_SYNCED=$(git log "$ref" -30 --format='%B' 2>/dev/null | grep -m1 '^Private-Commit:' | awk '{print $2}' || true)
        [[ -n "$LAST_SYNCED" ]] && break
    done
fi

if [[ -z "$LAST_SYNCED" ]]; then
    echo "错误：找不到 Private-Commit trailer，无法确定上次同步点"
    echo "提示：运行 init-public-pr.sh 初始化，或手动创建锚点 commit"
    exit 1
fi

echo "上次同步锚点: $LAST_SYNCED"

# ─── 收集待重放 commit ─────────────────────────────────────────────

COMMITS=()
while IFS= read -r hash; do
    [[ -n "$hash" ]] && COMMITS+=("$hash")
done < <(git -C "$PRIVATE" rev-list --reverse "${LAST_SYNCED}..${PRIVATE_BRANCH}")

if [[ ${#COMMITS[@]} -eq 0 ]]; then
    echo "没有新 commit 需要同步"
    exit 0
fi

echo "找到 ${#COMMITS[@]} 个待同步 commit"
echo ""

# ─── 按 scope 分组 ─────────────────────────────────────────────────
#
# 解析规则（按 conventional commit）：
#   feat(<scope>): xxx      → scope
#   fix(<scope>): xxx       → scope
#   docs(<scope>): xxx      → scope
#   <任意中文>(<scope>): xxx → scope（兼容 "修复(web)" 等）
#   feat: xxx (无括号)      → misc
#   其它（Merge / Revert）  → misc

parse_scope() {
    local subject="$1"
    # 提取第一个 `xxxx(scope):` 形式中的 scope
    local scope
    scope=$(echo "$subject" | sed -nE 's/^[^(]*\(([^)]+)\):.*/\1/p')
    if [[ -z "$scope" ]]; then
        echo "$MISC_SCOPE"
    else
        # scope 内有逗号 / 斜杠 → 取第一个（跨域 commit 归到主 scope）
        echo "$scope" | sed -E 's/[,/ ].*//'
    fi
}

# macOS 默认 bash 3.2 不支持关联数组，用两个并行数组替代：
#   - SCOPE_ORDER[]    去重 + 保序的 scope 名
#   - COMMIT_HASHES[] / COMMIT_SCOPES[]   每个 commit 对应的 scope
SCOPE_ORDER=()
COMMIT_HASHES=()
COMMIT_SCOPES=()

scope_seen() {
    local s="$1"
    for existing in "${SCOPE_ORDER[@]}"; do
        [[ "$existing" == "$s" ]] && return 0
    done
    return 1
}

for hash in "${COMMITS[@]}"; do
    subject=$(git -C "$PRIVATE" log -1 --format='%s' "$hash")
    scope=$(parse_scope "$subject")
    scope_seen "$scope" || SCOPE_ORDER+=("$scope")
    COMMIT_HASHES+=("$hash")
    COMMIT_SCOPES+=("$scope")
done

# 打印分组计划
echo "──── 分组计划 ────"
for scope in "${SCOPE_ORDER[@]}"; do
    count=0
    for i in "${!COMMIT_HASHES[@]}"; do
        [[ "${COMMIT_SCOPES[$i]}" == "$scope" ]] && count=$((count + 1))
    done
    echo ""
    echo "📦 dev-${scope}  (${count} commits)"
    for i in "${!COMMIT_HASHES[@]}"; do
        if [[ "${COMMIT_SCOPES[$i]}" == "$scope" ]]; then
            echo "    $(git -C "$PRIVATE" log -1 --format='%h %s' "${COMMIT_HASHES[$i]}" | cut -c1-100)"
        fi
    done
done
echo ""
echo "─────────────────"
echo ""

if $DRY_RUN; then
    echo "✅ DRY-RUN 结束。无任何远端改动。"
    exit 0
fi

# ─── 真跑：每个 scope 一个分支 ─────────────────────────────────────

PRIVATE_HEAD=$(git -C "$PRIVATE" rev-parse "$PRIVATE_BRANCH")
TOTAL_SYNCED=0
CREATED_PRS=()
UPDATED_PRS=()

NUM_SCOPES=${#SCOPE_ORDER[@]}

idx=0
for scope in "${SCOPE_ORDER[@]}"; do
    idx=$((idx + 1))
    dev_branch="dev-${scope}"
    is_last_scope=$([ $idx -eq $NUM_SCOPES ] && echo true || echo false)

    echo ""
    echo "════════ [${idx}/${NUM_SCOPES}] 处理 scope: ${scope}（分支 ${dev_branch}）════════"

    # 1. 切到该分支：远端有 → reset 跟随；远端无 → 基于 main 创建
    cd "$PUBLIC"
    git checkout main 2>/dev/null
    git pull "$PUBLIC_REMOTE" main --ff-only 2>/dev/null || true

    if git rev-parse --verify "$PUBLIC_REMOTE/$dev_branch" >/dev/null 2>&1; then
        # 远端分支存在 → 看看是否有对应 open PR
        existing_pr=$(gh pr list --repo "$GITHUB_REPO" --head "$dev_branch" --base main --state open --json number -q '.[0].number' 2>/dev/null || true)
        if [[ -n "$existing_pr" ]]; then
            echo "  发现 open PR #${existing_pr}，将追加 commit 到现有分支"
            git checkout "$dev_branch" 2>/dev/null || git checkout -b "$dev_branch" "$PUBLIC_REMOTE/$dev_branch"
            git reset --hard "$PUBLIC_REMOTE/$dev_branch" 2>/dev/null || true
            DEV_EXISTS=true
        else
            echo "  远端分支存在但 PR 已关 / 未开，基于 main 重建"
            git checkout -B "$dev_branch" main
            DEV_EXISTS=false
        fi
    else
        echo "  远端分支不存在，基于 main 创建"
        git checkout -B "$dev_branch" main
        DEV_EXISTS=false
    fi

    # 2. 逐 commit 重放该 scope 的所有 commit（按原顺序）
    scope_synced=0
    scope_hashes=()
    for i in "${!COMMIT_HASHES[@]}"; do
        if [[ "${COMMIT_SCOPES[$i]}" == "$scope" ]]; then
            scope_hashes+=("${COMMIT_HASHES[$i]}")
        fi
    done

    for hash in "${scope_hashes[@]}"; do
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
        scope_synced=$((scope_synced + 1))
        echo "    ✓ $(git -C "$PRIVATE" log -1 --format='%h %s' "$hash" | cut -c1-100)"
    done

    if [[ $scope_synced -eq 0 ]]; then
        echo "  该 scope 白名单内无变更，跳过"
        if [[ "$DEV_EXISTS" == false ]]; then
            git checkout main 2>/dev/null
            git branch -D "$dev_branch" 2>/dev/null || true
        fi
        continue
    fi

    # 3. 最后一个 scope 的最后一个 commit 加 Private-Commit trailer（推进同步锚点）
    if $is_last_scope; then
        LAST_MSG=$(git log -1 --format='%B')
        git commit --amend --no-verify --no-edit -m "${LAST_MSG%$'\n'}

Private-Commit: $PRIVATE_HEAD"
        echo "  追加 Private-Commit trailer: $PRIVATE_HEAD"
    fi

    # 4. push
    git push "$PUBLIC_REMOTE" "$dev_branch" -f
    echo "  推送 $dev_branch 完成"

    # 5. 创建/追加 PR
    existing_pr=$(gh pr list --repo "$GITHUB_REPO" --head "$dev_branch" --base main --state open --json number -q '.[0].number' 2>/dev/null || true)

    if [[ -n "$existing_pr" ]]; then
        UPDATED_PRS+=("#${existing_pr} (${scope}): +${scope_synced} commits")
        echo "  ↪ 已有 PR #${existing_pr}，追加了 ${scope_synced} 个 commit"
    else
        PR_TITLE="${scope}: $(date +%Y-%m-%d) sync"
        COMMIT_LOG=$(git log "main..$dev_branch" --oneline | head -50)

        PR_URL=$(gh pr create \
            --base main \
            --head "$dev_branch" \
            --title "$PR_TITLE" \
            --body "$(cat <<EOF
## Scope
\`${scope}\`

## Commits
${COMMIT_LOG}

---
Private-Commit: \`${PRIVATE_HEAD}\`
EOF
)" \
            --repo "$GITHUB_REPO" 2>&1)

        pr_num=$(echo "$PR_URL" | grep -oE '/pull/[0-9]+' | grep -oE '[0-9]+' | head -1)
        CREATED_PRS+=("#${pr_num} (${scope}): ${scope_synced} commits")
        echo "  ✨ 创建新 PR: $PR_URL"
    fi

    TOTAL_SYNCED=$((TOTAL_SYNCED + scope_synced))
done

# ─── 汇总 ───────────────────────────────────────────────────────────

echo ""
echo "════════════════ 同步完成 ════════════════"
echo "总计：${TOTAL_SYNCED} 个 commit 同步到 ${NUM_SCOPES} 个 scope"
echo ""

if [[ ${#CREATED_PRS[@]} -gt 0 ]]; then
    echo "📌 新建 PR：${#CREATED_PRS[@]} 个"
    for pr in "${CREATED_PRS[@]}"; do echo "    $pr"; done
fi

if [[ ${#UPDATED_PRS[@]} -gt 0 ]]; then
    echo "🔄 追加到已有 PR：${#UPDATED_PRS[@]} 个"
    for pr in "${UPDATED_PRS[@]}"; do echo "    $pr"; done
fi
