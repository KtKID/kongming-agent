#!/usr/bin/env bash
set -euo pipefail

: "${PR_NUMBER:?PR_NUMBER is required}"
: "${MINIMAX_API_KEY:?MINIMAX_API_KEY is required}"
: "${MINIMAX_API_URL:=https://api.minimaxi.com/anthropic}"
: "${MINIMAX_MODEL:=MiniMax-M3}"
: "${MINIMAX_MAX_TOKENS:=8192}"

MAX_DIFF_CHARS=80000

# ── 1. 收集 PR 信息 ──

PR_TITLE=$(gh pr view "$PR_NUMBER" --json title -q .title)
PR_BODY=$(gh pr view "$PR_NUMBER" --json body -q .body)
DIFF=$(gh pr diff "$PR_NUMBER" || true)

if [ -z "$DIFF" ]; then
  gh pr comment "$PR_NUMBER" --body "> 🤖 自动审查：PR diff 为空，跳过审查。"
  exit 0
fi

if [ ${#DIFF} -gt $MAX_DIFF_CHARS ]; then
  DIFF="${DIFF:0:$MAX_DIFF_CHARS}

... (diff 过大，已截断前 ${MAX_DIFF_CHARS} 字符)"
fi

# ── 2. 构建请求 ──

SYSTEM_PROMPT='你是一个严格的代码审查专家。审查以下 PR 的 diff，输出结构化审查报告。

审查维度：
1. **Bug 风险**：逻辑错误、边界条件、空指针、竞态
2. **安全隐患**：注入、信息泄露、权限绕过
3. **性能问题**：N+1 查询、不必要的循环、大对象拷贝
4. **代码质量**：命名、可读性、重复代码

输出格式：
- 每个问题用 `### [严重度] 文件:行号` 标题
- 严重度分三级：🔴 Critical / 🟡 Major / 🔵 Minor
- 给出具体修复建议
- 如果没有问题，直接说"未发现明显问题，LGTM ✅"
- 最后给一个总结：是否建议合并'

USER_PROMPT="PR Title: ${PR_TITLE}
PR Description: ${PR_BODY:-无}

Diff:
\`\`\`diff
${DIFF}
\`\`\`"

PAYLOAD=$(jq -n \
  --arg model "$MINIMAX_MODEL" \
  --arg system "$SYSTEM_PROMPT" \
  --arg user "$USER_PROMPT" \
  --argjson max_tokens "$MINIMAX_MAX_TOKENS" \
  '{
    model: $model,
    max_tokens: $max_tokens,
    system: $system,
    messages: [
      {role: "user", content: $user}
    ]
  }')

# ── 3. 调用 API ──

HTTP_RESPONSE=$(curl -s -w "\n%{http_code}" \
  "${MINIMAX_API_URL}/v1/messages" \
  -H "x-api-key: ${MINIMAX_API_KEY}" \
  -H "content-type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d "$PAYLOAD")

HTTP_CODE=$(echo "$HTTP_RESPONSE" | tail -1)
BODY=$(echo "$HTTP_RESPONSE" | sed '$d')

if [ "$HTTP_CODE" -ne 200 ]; then
  echo "::error::MiniMax API returned HTTP ${HTTP_CODE}"
  echo "$BODY"
  exit 1
fi

# ── 4. 解析并发评论 ──

REVIEW=$(echo "$BODY" | jq -r '[.content[]? | select(.type == "text") | .text] | join("\n\n")')

if [ -z "$REVIEW" ]; then
  STOP_REASON=$(echo "$BODY" | jq -r '.stop_reason // "unknown"')
  CONTENT_TYPES=$(echo "$BODY" | jq -r '[.content[]?.type] | unique | join(", ")')
  COMMENT="## 🤖 LLM Code Review

自动审查没有生成可发布的文本内容。

- stop_reason: ${STOP_REASON}
- content_types: ${CONTENT_TYPES:-none}

请检查 MiniMax 响应预算或 thinking/text 输出配置。

---
<sub>Reviewed by MiniMax (${MINIMAX_MODEL}) · PR #${PR_NUMBER}</sub>"

  gh pr comment "$PR_NUMBER" --body "$COMMENT"
  echo "::error::API response has no text content"
  echo "$BODY"
  exit 1
fi

COMMENT="## 🤖 LLM Code Review

${REVIEW}

---
<sub>Reviewed by MiniMax (${MINIMAX_MODEL}) · PR #${PR_NUMBER}</sub>"

gh pr comment "$PR_NUMBER" --body "$COMMENT"
echo "Review posted to PR #${PR_NUMBER}"
