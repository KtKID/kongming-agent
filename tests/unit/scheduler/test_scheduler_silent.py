"""unit：scheduler.silent 纯函数覆盖（Wave B2）。

覆盖矩阵：
- ``is_silent``: None / 空串 / lstrip 后空 / 命中 / 大小写不敏感 / 非首位不算
- ``strip_silent_prefix``: 命中后剥离 + 后续 lstrip / 不命中原样返回 / 单独 ``[SILENT]``
"""

from __future__ import annotations

import pytest

from scheduler.silent import is_silent, strip_silent_prefix

# ---------------------------------------------------------------------------
# is_silent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, False),
        ("", False),
        ("   ", False),  # lstrip 后空
        ("\n\t  ", False),
        ("[SILENT]", True),
        ("[silent]", True),
        ("[Silent]\n", True),
        ("  [SILENT] no diff", True),  # 前导空白 lstrip
        ("\n[silent]", True),
        ("[SILENT] no news today", True),
        ("hello [SILENT]", False),  # marker 不在开头
        ("text [silent] more", False),
        ("plain message", False),
    ],
)
def test_is_silent(text: str | None, expected: bool) -> None:
    assert is_silent(text) is expected


# ---------------------------------------------------------------------------
# strip_silent_prefix
# ---------------------------------------------------------------------------


def test_strip_silent_prefix_simple() -> None:
    """命中：剥前缀 + 对剩余部分 lstrip。"""
    assert strip_silent_prefix("[SILENT] no news") == "no news"


def test_strip_silent_prefix_lowercase() -> None:
    assert strip_silent_prefix("[silent] body") == "body"


def test_strip_silent_prefix_only_marker() -> None:
    """单独 ``[SILENT]`` → 剥完为空串。"""
    assert strip_silent_prefix("[SILENT]") == ""


def test_strip_silent_prefix_with_leading_whitespace() -> None:
    """前置空白下 head 也命中：实现保留原 leading whitespace（``[:idx]``），
    然后剥掉 marker 本身，对其后内容 lstrip。"""
    result = strip_silent_prefix("  [silent]\nbody")
    # 实现：final_message[:idx] + final_message[idx + len(MARKER):].lstrip()
    # idx 是 upper().find("[SILENT]") = 2，所以保留前 2 个空格
    assert result == "  body"


def test_strip_silent_prefix_with_newline_after_marker() -> None:
    assert strip_silent_prefix("[SILENT]\nrest") == "rest"


def test_strip_silent_prefix_no_match_returns_original() -> None:
    assert strip_silent_prefix("hello") == "hello"


def test_strip_silent_prefix_marker_not_at_head() -> None:
    """marker 不在开头 → head 检查失败，原样返回。"""
    text = "hello [SILENT] world"
    assert strip_silent_prefix(text) == text


def test_strip_silent_prefix_empty_string() -> None:
    """空串：head[:7] == "" != "[SILENT]" → 原样返回。"""
    assert strip_silent_prefix("") == ""


def test_strip_silent_prefix_mixed_case() -> None:
    assert strip_silent_prefix("[Silent] mixed") == "mixed"
