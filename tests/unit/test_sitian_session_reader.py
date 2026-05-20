"""unit：sitian.session_reader.read_claude_session_tail。

覆盖：
- 默认返回最近 1 user + 1 assistant
- 自定义 user_count / assistant_count
- max_chars 截断与省略号
- max_chars=0 关闭截断
- user_count=0 返回空 users
- 损坏行静默跳过
- structured list content 拼接 text 字段
- 文件不存在返回空
- 整个会话无 user
- 跳过 type=summary / type=system
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sitian.session_reader import read_claude_session_tail


def _make_jsonl(tmp_path: Path, entries: list[dict[str, Any]]) -> Path:
    """把 entries 逐行序列化写到 tmp_path/session.jsonl 并返回路径。"""

    p = tmp_path / "session.jsonl"
    lines = [json.dumps(e, ensure_ascii=False) for e in entries]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _user(text: str) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant(text: str) -> dict[str, Any]:
    return {"type": "assistant", "message": {"role": "assistant", "content": text}}


# ---------------------------------------------------------------------------
# 1. 默认参数：最近 1 user + 1 assistant
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reads_last_user_and_assistant_with_defaults(tmp_path: Path) -> None:
    path = _make_jsonl(
        tmp_path,
        [
            _user("u1"),
            _assistant("a1"),
            _user("u2"),
            _assistant("a2"),
            _user("u3-latest"),
            _assistant("a3-latest"),
        ],
    )
    result = read_claude_session_tail(path)
    assert result == {"users": ["u3-latest"], "assistants": ["a3-latest"]}


# ---------------------------------------------------------------------------
# 2. 自定义 count：top N from newest
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_respects_user_count_and_assistant_count(tmp_path: Path) -> None:
    entries: list[dict[str, Any]] = []
    # 交替写 5 user + 4 assistant（交替 + 收尾 user）
    for i in range(1, 6):
        entries.append(_user(f"u{i}"))
        if i <= 4:
            entries.append(_assistant(f"a{i}"))
    path = _make_jsonl(tmp_path, entries)

    result = read_claude_session_tail(path, user_count=3, assistant_count=2)
    assert result["users"] == ["u5", "u4", "u3"]
    assert result["assistants"] == ["a4", "a3"]


# ---------------------------------------------------------------------------
# 3. 截断 + 省略号
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_truncates_message_with_ellipsis(tmp_path: Path) -> None:
    long_text = "x" * 800
    path = _make_jsonl(tmp_path, [_user(long_text)])
    result = read_claude_session_tail(path, max_chars=100)

    assert len(result["users"]) == 1
    out = result["users"][0]
    # 长度 = 100 字符正文 + 单字符 …
    assert len(out) == 101
    assert out.endswith("…")
    assert out[:100] == long_text[:100]


# ---------------------------------------------------------------------------
# 4. max_chars=0 关闭截断
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_max_chars_zero_disables_truncation(tmp_path: Path) -> None:
    long_text = "y" * 1234
    path = _make_jsonl(tmp_path, [_user(long_text)])
    result = read_claude_session_tail(path, max_chars=0)

    assert result["users"] == [long_text]
    assert "…" not in result["users"][0]


# ---------------------------------------------------------------------------
# 5. user_count=0 → users 数组为空，assistant 不受影响
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_user_count_zero_returns_empty_users(tmp_path: Path) -> None:
    path = _make_jsonl(
        tmp_path,
        [
            _user("u1"),
            _assistant("a1"),
            _user("u2"),
            _assistant("a2"),
        ],
    )
    result = read_claude_session_tail(path, user_count=0, assistant_count=1)
    assert result["users"] == []
    assert result["assistants"] == ["a2"]


# ---------------------------------------------------------------------------
# 6. 损坏行静默 skip
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_skips_corrupted_lines(tmp_path: Path) -> None:
    p = tmp_path / "session.jsonl"
    lines = [
        json.dumps(_user("u-old")),
        "{not valid json",
        json.dumps(_assistant("a-mid")),
        "another corrupted ][",
        json.dumps(_user("u-new")),
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = read_claude_session_tail(p, user_count=2, assistant_count=2)
    assert result["users"] == ["u-new", "u-old"]
    assert result["assistants"] == ["a-mid"]


# ---------------------------------------------------------------------------
# 7. structured content: list[dict]，只拼 type=text
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handles_structured_content_list(tmp_path: Path) -> None:
    entry = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": "abc"},
                {"type": "tool_use", "id": "toolu_01"},
                {"type": "text", "text": "def"},
            ],
        },
    }
    path = _make_jsonl(tmp_path, [entry])
    result = read_claude_session_tail(path, user_count=1, assistant_count=0)
    assert result["users"] == ["abc def"]


# ---------------------------------------------------------------------------
# 8. 文件不存在 → 空 dict 不抛
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_returns_empty_for_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-file.jsonl"
    result = read_claude_session_tail(missing)
    assert result == {"users": [], "assistants": []}


# ---------------------------------------------------------------------------
# 9. 全是 assistant，users 为空
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_returns_empty_for_session_without_user(tmp_path: Path) -> None:
    path = _make_jsonl(
        tmp_path,
        [
            _assistant("a1"),
            _assistant("a2"),
            _assistant("a3-last"),
        ],
    )
    result = read_claude_session_tail(path, user_count=2, assistant_count=1)
    assert result["users"] == []
    assert result["assistants"] == ["a3-last"]


# ---------------------------------------------------------------------------
# 10. 跳过 type=summary / type=system 等非 chat 记录
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_skips_non_chat_types(tmp_path: Path) -> None:
    path = _make_jsonl(
        tmp_path,
        [
            {"type": "summary", "summary": "this is summary"},
            _user("u1"),
            {"type": "system", "message": {"content": "system msg"}},
            _assistant("a1"),
            {"type": "summary", "summary": "tail summary"},
        ],
    )
    result = read_claude_session_tail(path, user_count=2, assistant_count=2)
    assert result["users"] == ["u1"]
    assert result["assistants"] == ["a1"]
