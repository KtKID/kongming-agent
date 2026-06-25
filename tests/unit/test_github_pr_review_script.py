from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "review.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pr-review.yml"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_pr_review_workflow_uses_repository_configuration() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "secrets.REVIEW_API_KEY" in workflow
    assert "vars.PR_REVIEW_PROVIDER" in workflow
    assert "vars.PR_REVIEW_API_URL" in workflow
    assert "vars.PR_REVIEW_MODEL" in workflow
    assert "secrets.GLM_API_KEY" not in workflow
    assert "secrets.MINIMAX_API_KEY" not in workflow
    assert "secrets.PR_REVIEW_API_KEY" not in workflow
    assert "glm-5.2" not in workflow
    assert "api.z.ai" not in workflow
    assert "vars.PR_REVIEW_CONNECT_TIMEOUT" in workflow
    assert "vars.PR_REVIEW_MAX_TIME" in workflow

    max_tokens_match = re.search(
        r"REVIEW_MAX_TOKENS:\s*\$\{\{\s*vars\.PR_REVIEW_MAX_TOKENS\s*\|\|\s*'(?P<value>\d+)'\s*\}\}",
        workflow,
    )
    assert max_tokens_match is not None
    assert int(max_tokens_match.group("value")) == 128 * 1024


def test_review_script_uses_openai_compatible_provider(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    comment_file = tmp_path / "comment.md"
    request_url_file = tmp_path / "request-url.txt"
    request_headers_file = tmp_path / "request-headers.txt"
    request_payload_file = tmp_path / "request-payload.json"
    request_args_file = tmp_path / "request-args.txt"

    _write_executable(
        bin_dir / "gh",
        """#!/usr/bin/env bash
set -euo pipefail

case "$1 $2" in
  "pr view")
    if [[ "$*" == *"-q .title"* ]]; then
      printf '%s\\n' "测试 PR"
    else
      printf '%s\\n' "测试描述"
    fi
    ;;
  "pr diff")
    printf '%s\\n' "diff --git a/app.py b/app.py"
    printf '%s\\n' "+print('ok')"
    ;;
  "pr comment")
    printf '%s' "$5" > "$COMMENT_FILE"
    ;;
  *)
    echo "unexpected gh args: $*" >&2
    exit 2
    ;;
esac
""",
    )
    _write_executable(
        bin_dir / "curl",
        """#!/usr/bin/env bash
set -euo pipefail

url=""
payload=""
headers=""
printf '%s\n' "$@" > "$REQUEST_ARGS_FILE"
while [ "$#" -gt 0 ]; do
  case "$1" in
    -H)
      headers="${headers}${2}
"
      shift 2
      ;;
    -d)
      payload="$2"
      shift 2
      ;;
    http*)
      url="$1"
      shift
      ;;
    *)
      shift
      ;;
  esac
done

printf '%s' "$url" > "$REQUEST_URL_FILE"
printf '%s' "$headers" > "$REQUEST_HEADERS_FILE"
printf '%s' "$payload" > "$REQUEST_PAYLOAD_FILE"
printf '%s\\n200' '{"choices":[{"message":{"content":"### [🔵 Minor] app.py:1\\n检查通过"}}]}'
""",
    )

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "COMMENT_FILE": str(comment_file),
        "REQUEST_URL_FILE": str(request_url_file),
        "REQUEST_HEADERS_FILE": str(request_headers_file),
        "REQUEST_PAYLOAD_FILE": str(request_payload_file),
        "REQUEST_ARGS_FILE": str(request_args_file),
        "PR_NUMBER": "123",
        "REVIEW_PROVIDER": "openai_compatible",
        "REVIEW_API_KEY": "glm-secret",
        "REVIEW_API_URL": "https://api.z.ai/api/coding/paas/v4",
        "REVIEW_MODEL": "glm-5.2",
        "REVIEW_MAX_TOKENS": "4096",
        "REVIEW_CONNECT_TIMEOUT": "17",
        "REVIEW_MAX_TIME": "181",
    }

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert request_url_file.read_text(encoding="utf-8") == (
        "https://api.z.ai/api/coding/paas/v4/chat/completions"
    )
    request_args = request_args_file.read_text(encoding="utf-8")
    assert "--connect-timeout\n17\n" in request_args
    assert "--max-time\n181\n" in request_args
    assert "Authorization: Bearer glm-secret" in request_headers_file.read_text(encoding="utf-8")

    payload = json.loads(request_payload_file.read_text(encoding="utf-8"))
    assert payload["model"] == "glm-5.2"
    assert payload["max_tokens"] == 4096
    assert [message["role"] for message in payload["messages"]] == ["system", "user"]

    comment = comment_file.read_text(encoding="utf-8")
    assert "### [🔵 Minor] app.py:1" in comment
    assert "Reviewed by openai_compatible (glm-5.2)" in comment


def test_review_script_rejects_invalid_max_tokens(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    _write_executable(
        bin_dir / "gh",
        """#!/usr/bin/env bash
set -euo pipefail

case "$1 $2" in
  "pr view")
    if [[ "$*" == *"-q .title"* ]]; then
      printf '%s\\n' "测试 PR"
    else
      printf '%s\\n' "测试描述"
    fi
    ;;
  "pr diff")
    printf '%s\\n' "diff --git a/app.py b/app.py"
    printf '%s\\n' "+print('ok')"
    ;;
  *)
    echo "unexpected gh args: $*" >&2
    exit 2
    ;;
esac
""",
    )

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "PR_NUMBER": "123",
        "REVIEW_PROVIDER": "openai_compatible",
        "REVIEW_API_KEY": "glm-secret",
        "REVIEW_API_URL": "https://api.z.ai/api/coding/paas/v4",
        "REVIEW_MODEL": "glm-5.2",
        "REVIEW_MAX_TOKENS": "bad-value",
    }

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "REVIEW_MAX_TOKENS must be a valid number" in result.stdout


def test_review_script_reports_openai_finish_reason_for_empty_content(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    comment_file = tmp_path / "comment.md"

    _write_executable(
        bin_dir / "gh",
        """#!/usr/bin/env bash
set -euo pipefail

case "$1 $2" in
  "pr view")
    if [[ "$*" == *"-q .title"* ]]; then
      printf '%s\\n' "测试 PR"
    else
      printf '%s\\n' "测试描述"
    fi
    ;;
  "pr diff")
    printf '%s\\n' "diff --git a/app.py b/app.py"
    printf '%s\\n' "+print('ok')"
    ;;
  "pr comment")
    printf '%s' "$5" > "$COMMENT_FILE"
    ;;
  *)
    echo "unexpected gh args: $*" >&2
    exit 2
    ;;
esac
""",
    )
    _write_executable(
        bin_dir / "curl",
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n200' '{"choices":[{"finish_reason":"length","message":{"content":""}}]}'
""",
    )

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "COMMENT_FILE": str(comment_file),
        "PR_NUMBER": "123",
        "REVIEW_PROVIDER": "openai_compatible",
        "REVIEW_API_KEY": "glm-secret",
        "REVIEW_API_URL": "https://api.z.ai/api/coding/paas/v4",
        "REVIEW_MODEL": "glm-5.2",
        "REVIEW_MAX_TOKENS": "4096",
    }

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    comment = comment_file.read_text(encoding="utf-8")
    assert "- stop_reason: length" in comment
    assert "- content_types: choices" in comment


def test_review_script_uses_anthropic_provider(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    comment_file = tmp_path / "comment.md"
    request_url_file = tmp_path / "request-url.txt"
    request_headers_file = tmp_path / "request-headers.txt"
    request_payload_file = tmp_path / "request-payload.json"
    request_args_file = tmp_path / "request-args.txt"

    _write_executable(
        bin_dir / "gh",
        """#!/usr/bin/env bash
set -euo pipefail

case "$1 $2" in
  "pr view")
    if [[ "$*" == *"-q .title"* ]]; then
      printf '%s\\n' "测试 PR"
    else
      printf '%s\\n' "测试描述"
    fi
    ;;
  "pr diff")
    printf '%s\\n' "diff --git a/app.py b/app.py"
    printf '%s\\n' "+print('ok')"
    ;;
  "pr comment")
    printf '%s' "$5" > "$COMMENT_FILE"
    ;;
  *)
    echo "unexpected gh args: $*" >&2
    exit 2
    ;;
esac
""",
    )
    _write_executable(
        bin_dir / "curl",
        """#!/usr/bin/env bash
set -euo pipefail

url=""
payload=""
headers=""
printf '%s\n' "$@" > "$REQUEST_ARGS_FILE"
while [ "$#" -gt 0 ]; do
  case "$1" in
    -H)
      headers="${headers}${2}
"
      shift 2
      ;;
    -d)
      payload="$2"
      shift 2
      ;;
    http*)
      url="$1"
      shift
      ;;
    *)
      shift
      ;;
  esac
done

printf '%s' "$url" > "$REQUEST_URL_FILE"
printf '%s' "$headers" > "$REQUEST_HEADERS_FILE"
printf '%s' "$payload" > "$REQUEST_PAYLOAD_FILE"
printf '%s\\n200' '{"content":[{"type":"text","text":"### [🔵 Minor] app.py:1\\n检查通过"}]}'
""",
    )

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "COMMENT_FILE": str(comment_file),
        "REQUEST_URL_FILE": str(request_url_file),
        "REQUEST_HEADERS_FILE": str(request_headers_file),
        "REQUEST_PAYLOAD_FILE": str(request_payload_file),
        "REQUEST_ARGS_FILE": str(request_args_file),
        "PR_NUMBER": "123",
        "REVIEW_PROVIDER": "anthropic",
        "REVIEW_API_KEY": "minimax-secret",
        "REVIEW_API_URL": "https://api.minimaxi.com/anthropic",
        "REVIEW_MODEL": "MiniMax-M3",
        "REVIEW_MAX_TOKENS": "4096",
        "REVIEW_CONNECT_TIMEOUT": "19",
        "REVIEW_MAX_TIME": "182",
    }

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert request_url_file.read_text(encoding="utf-8") == (
        "https://api.minimaxi.com/anthropic/v1/messages"
    )
    request_args = request_args_file.read_text(encoding="utf-8")
    assert "--connect-timeout\n19\n" in request_args
    assert "--max-time\n182\n" in request_args
    headers = request_headers_file.read_text(encoding="utf-8")
    assert "x-api-key: minimax-secret" in headers
    assert "anthropic-version: 2023-06-01" in headers

    payload = json.loads(request_payload_file.read_text(encoding="utf-8"))
    assert payload["model"] == "MiniMax-M3"
    assert payload["system"].startswith("你是一个严格的代码审查专家")
    assert [message["role"] for message in payload["messages"]] == ["user"]

    comment = comment_file.read_text(encoding="utf-8")
    assert "### [🔵 Minor] app.py:1" in comment
    assert "Reviewed by anthropic (MiniMax-M3)" in comment
