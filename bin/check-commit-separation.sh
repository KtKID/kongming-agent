#!/usr/bin/env bash
# 拦截代码与文档混合提交。
# 代码：src/ tests/ web/src/ 下的 .py .ts .tsx
# 文档：docs/ dev-pipeline/ 下的 .md
#
# 同时出现两类则拒绝提交，提示拆成两笔。

set -euo pipefail

staged=$(git -c core.quotepath=false diff --cached --name-only --diff-filter=ACMR)
[ -z "$staged" ] && exit 0

has_code=false
has_docs=false
code_files=()
doc_files=()

while IFS= read -r f; do
  if [[ "$f" =~ ^(src|tests)/.*\.py$ ]] || [[ "$f" =~ ^web/src/.*\.(ts|tsx)$ ]]; then
    has_code=true
    code_files+=("$f")
  elif [[ "$f" =~ ^(docs|dev-pipeline)/.*\.md$ ]]; then
    has_docs=true
    doc_files+=("$f")
  fi
done <<< "$staged"

if $has_code && $has_docs; then
  echo "❌ 代码和文档不能混在同一个 commit"
  echo ""
  echo "代码文件："
  for f in "${code_files[@]}"; do echo "  $f"; done
  echo ""
  echo "文档文件："
  for f in "${doc_files[@]}"; do echo "  $f"; done
  echo ""
  echo "请拆成两笔提交：先提交一类，再提交另一类。"
  exit 1
fi
