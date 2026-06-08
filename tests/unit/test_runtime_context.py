"""unit：prompting.runtime_context 文本生成覆盖。

验证 ``build_runtime_context_text`` 输出包含 cwd 与 kongming_home 两个绝对
路径，并保持稳定的标题 / 提示语，便于 LLM 锚定路径语义。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prompting.runtime_context import build_runtime_context_text


@pytest.mark.unit
def test_text_contains_cwd_and_home_paths(tmp_path: Path) -> None:
    """两个路径都应原样落在文本里。"""
    cwd = tmp_path / "proj"
    home = tmp_path / "proj" / ".kongming"

    text = build_runtime_context_text(cwd=cwd, kongming_home=home)

    assert str(cwd) in text
    assert str(home) in text


@pytest.mark.unit
def test_text_has_stable_section_header() -> None:
    """标题用于让 LLM 在 system prompt 中识别"运行时环境"段。"""
    text = build_runtime_context_text(
        cwd=Path("/a"),
        kongming_home=Path("/a/.kongming"),
    )

    assert text.startswith("## 你的工作根")


@pytest.mark.unit
def test_text_explains_path_semantics() -> None:
    """提示语必须用真实目录名（cwd / .kongming），不暴露内部变量名。"""
    text = build_runtime_context_text(
        cwd=Path("/a"),
        kongming_home=Path("/a/.kongming"),
    )

    assert "cwd" in text
    assert ".kongming" in text
    # 内部变量名 `kongming_home` 不应该泄到给 LLM 看的文本里
    assert "kongming_home" not in text


@pytest.mark.unit
def test_text_is_stripped_and_non_empty() -> None:
    """函数输出已 strip，可直接进 InstructionSource。"""
    text = build_runtime_context_text(
        cwd=Path("/x"),
        kongming_home=Path("/x/.kongming"),
    )

    assert text == text.strip()
    assert text


@pytest.mark.unit
def test_paths_with_spaces_are_preserved(tmp_path: Path) -> None:
    """带空格的路径不应被破坏。"""
    cwd = tmp_path / "my proj"
    home = cwd / ".kongming"

    text = build_runtime_context_text(cwd=cwd, kongming_home=home)

    assert str(cwd) in text
    assert str(home) in text
