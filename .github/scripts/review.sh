#!/usr/bin/env bash
# PR 自动审核脚本：读取当前 PR 标题、正文和 diff，按 REVIEW_PROVIDER 组装 LLM 请求，
# 调用配置的审核模型生成审查报告，并通过 gh CLI 回写 PR 评论。关键流程：
# 1. 使用 gh 收集 PR 元信息和 diff；
# 2. 根据 REVIEW_PROVIDER 构建 OpenAI-compatible 或 Anthropic 请求；
# 3. 使用 curl 调用模型 API；
# 4. 解析模型文本并发布 GitHub PR 评论。
set -euo pipefail

: "${PR_NUMBER:?PR_NUMBER is required}"
: "${REVIEW_API_KEY:?REVIEW_API_KEY is required}"
: "${REVIEW_API_URL:?REVIEW_API_URL is required}"
: "${REVIEW_MODEL:?REVIEW_MODEL is required}"
: "${REVIEW_PROVIDER:=openai_compatible}"
: "${REVIEW_MAX_TOKENS:=131072}"

if ! [[ "$REVIEW_MAX_TOKENS" =~ ^[0-9]+$ ]]; then
  echo "::error::REVIEW_MAX_TOKENS must be a valid number, got: ${REVIEW_MAX_TOKENS}"
  exit 1
fi

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
  --arg model "$REVIEW_MODEL" \
  --arg system "$SYSTEM_PROMPT" \
  --arg user "$USER_PROMPT" \
  --argjson max_tokens "$REVIEW_MAX_TOKENS" \
  '{
    model: $model,
    max_tokens: $max_tokens,
    messages: [
      {role: "system", content: $system},
      {role: "user", content: $user}
    ]
  }')

# ── 3. 调用 API ──

API_BASE="${REVIEW_API_URL%/}"

if [ "$REVIEW_PROVIDER" = "openai_compatible" ]; then
  HTTP_RESPONSE=$(curl -s -w "\n%{http_code}" \
    "${API_BASE}/chat/completions" \
    -H "Authorization: Bearer ${REVIEW_API_KEY}" \
    -H "content-type: application/json" \
    -d "$PAYLOAD")
elif [ "$REVIEW_PROVIDER" = "anthropic" ]; then
  ANTHROPIC_PAYLOAD=$(jq -n \
    --arg model "$REVIEW_MODEL" \
    --arg system "$SYSTEM_PROMPT" \
    --arg user "$USER_PROMPT" \
    --argjson max_tokens "$REVIEW_MAX_TOKENS" \
    '{
      model: $model,
      max_tokens: $max_tokens,
      system: $system,
      messages: [
        {role: "user", content: $user}
      ]
    }')
  HTTP_RESPONSE=$(curl -s -w "\n%{http_code}" \
    "${API_BASE}/v1/messages" \
    -H "x-api-key: ${REVIEW_API_KEY}" \
    -H "content-type: application/json" \
    -H "anthropic-version: 2023-06-01" \
    -d "$ANTHROPIC_PAYLOAD")
else
  echo "::error::Unsupported REVIEW_PROVIDER: ${REVIEW_PROVIDER}"
  exit 1
fi

HTTP_CODE=$(echo "$HTTP_RESPONSE" | tail -1)
BODY=$(echo "$HTTP_RESPONSE" | sed '$d')

if [ "$HTTP_CODE" -ne 200 ]; then
  echo "::error::Review API returned HTTP ${HTTP_CODE}"
  echo "$BODY"
  exit 1
fi

# ── 4. 解析并发评论 ──

if [ "$REVIEW_PROVIDER" = "openai_compatible" ]; then
  REVIEW=$(echo "$BODY" | jq -r '.choices[0].message.content // ""')
else
  REVIEW=$(echo "$BODY" | jq -r '[.content[]? | select(.type == "text") | .text] | join("\n\n")')
fi

if [ -z "$REVIEW" ]; then
  STOP_REASON=$(echo "$BODY" | jq -r 'if .choices then (.choices[0].finish_reason // "unknown") else (.stop_reason // "unknown") end')
  CONTENT_TYPES=$(echo "$BODY" | jq -r 'if .choices then "choices" else ([.content[]?.type] | unique | join(", ")) end')
  COMMENT="## 🤖 LLM Code Review

自动审查没有生成可发布的文本内容。

- stop_reason: ${STOP_REASON}
- content_types: ${CONTENT_TYPES:-none}

请检查审核模型响应预算或 thinking/text 输出配置。

---
<sub>Reviewed by ${REVIEW_PROVIDER} (${REVIEW_MODEL}) · PR #${PR_NUMBER}</sub>"

  gh pr comment "$PR_NUMBER" --body "$COMMENT"
  echo "::error::API response has no text content"
  echo "$BODY"
  exit 1
fi

COMMENT="## 🤖 LLM Code Review

${REVIEW}

---
<sub>Reviewed by ${REVIEW_PROVIDER} (${REVIEW_MODEL}) · PR #${PR_NUMBER}</sub>"

gh pr comment "$PR_NUMBER" --body "$COMMENT"
echo "Review posted to PR #${PR_NUMBER}"
