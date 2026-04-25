"""unit：InstructionLoader 三类来源加载、缺失语义、render 规则契约。

覆盖范围（对应 dev-checklist.md #2 #3 #4 #5）：

#2 三类来源加载（agent_spec / extra_files / env）
#3 缺失语义（文件不存在/空文件/全空白/OSError 跳过，UnicodeDecodeError 冒泡）
#4 render() 规则（# origin 前缀、空行分隔、空来源过滤、顺序稳定）
#5 同名文件 origin 冲突（两条相同 origin 的 source 都被保留，锁死已知限制）
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from context.instruction_loader import InstructionLoader, InstructionSource

# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------


def _run(coro):  # type: ignore[no-untyped-def]
    """在测试中同步运行 async 函数。"""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# #2 三类来源加载
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_agent_spec_non_empty_enters_sources() -> None:
    """agent_spec 非空时，sources 包含 origin='agent_spec' 的条目。"""
    loader = InstructionLoader(include_env=False)
    sources = _run(loader.load("Be helpful."))
    origins = [s.origin for s in sources]
    assert "agent_spec" in origins
    assert sources[0].content == "Be helpful."


@pytest.mark.unit
def test_agent_spec_empty_skipped() -> None:
    """agent_spec 为空字符串时，不进入 sources。"""
    loader = InstructionLoader(include_env=False)
    sources = _run(loader.load(""))
    assert all(s.origin != "agent_spec" for s in sources)


@pytest.mark.unit
def test_agent_spec_whitespace_only_skipped() -> None:
    """agent_spec 全空白时，不进入 sources。"""
    loader = InstructionLoader(include_env=False)
    sources = _run(loader.load("   \n  "))
    assert all(s.origin != "agent_spec" for s in sources)


@pytest.mark.unit
def test_agent_spec_none_skipped() -> None:
    """agent_spec 为 None 时，不进入 sources。"""
    loader = InstructionLoader(include_env=False)
    sources = _run(loader.load(None))
    assert all(s.origin != "agent_spec" for s in sources)


@pytest.mark.unit
def test_extra_files_multiple_in_order(tmp_path: Path) -> None:
    """extra_files 多文件按传入顺序进入 sources，origin 为 file:<filename>。"""
    file_a = tmp_path / "alpha.md"
    file_b = tmp_path / "beta.md"
    file_a.write_text("content a", encoding="utf-8")
    file_b.write_text("content b", encoding="utf-8")

    loader = InstructionLoader(extra_files=[file_a, file_b], include_env=False)
    sources = _run(loader.load(None))

    assert len(sources) == 2
    assert sources[0].origin == "file:alpha.md"
    assert sources[0].content == "content a"
    assert sources[1].origin == "file:beta.md"
    assert sources[1].content == "content b"


@pytest.mark.unit
def test_env_var_non_empty_enters_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """env var KONGMING_EXTRA_INSTRUCTIONS 非空时，sources 包含 origin='env:KONGMING_EXTRA_INSTRUCTIONS'。"""
    monkeypatch.setenv("KONGMING_EXTRA_INSTRUCTIONS", "extra rules here")
    loader = InstructionLoader(include_env=True)
    sources = _run(loader.load(None))

    env_sources = [s for s in sources if s.origin == "env:KONGMING_EXTRA_INSTRUCTIONS"]
    assert len(env_sources) == 1
    assert env_sources[0].content == "extra rules here"


@pytest.mark.unit
def test_env_var_missing_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """env var 不存在时，sources 不含 env 条目。"""
    monkeypatch.delenv("KONGMING_EXTRA_INSTRUCTIONS", raising=False)
    loader = InstructionLoader(include_env=True)
    sources = _run(loader.load(None))
    assert all("env:" not in s.origin for s in sources)


@pytest.mark.unit
def test_env_var_empty_string_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """env var 为空字符串时，sources 不含 env 条目。"""
    monkeypatch.setenv("KONGMING_EXTRA_INSTRUCTIONS", "")
    loader = InstructionLoader(include_env=True)
    sources = _run(loader.load(None))
    assert all("env:" not in s.origin for s in sources)


@pytest.mark.unit
def test_env_var_whitespace_only_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """env var 全空白时，sources 不含 env 条目。"""
    monkeypatch.setenv("KONGMING_EXTRA_INSTRUCTIONS", "   \n  ")
    loader = InstructionLoader(include_env=True)
    sources = _run(loader.load(None))
    assert all("env:" not in s.origin for s in sources)


# ---------------------------------------------------------------------------
# #3 缺失语义
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_file_skipped(tmp_path: Path) -> None:
    """文件不存在时跳过，不进入 sources。"""
    missing = tmp_path / "nonexistent.md"
    loader = InstructionLoader(extra_files=[missing], include_env=False)
    sources = _run(loader.load(None))
    assert sources == []


@pytest.mark.unit
def test_empty_file_skipped(tmp_path: Path) -> None:
    """文件存在但为空时跳过，不进入 sources。"""
    empty = tmp_path / "empty.md"
    empty.write_text("", encoding="utf-8")
    loader = InstructionLoader(extra_files=[empty], include_env=False)
    sources = _run(loader.load(None))
    assert sources == []


@pytest.mark.unit
def test_whitespace_only_file_skipped(tmp_path: Path) -> None:
    """文件存在但全空白时跳过，不进入 sources。"""
    ws_file = tmp_path / "whitespace.md"
    ws_file.write_text("   \n\t\n  ", encoding="utf-8")
    loader = InstructionLoader(extra_files=[ws_file], include_env=False)
    sources = _run(loader.load(None))
    assert sources == []


@pytest.mark.unit
def test_oserror_skipped(tmp_path: Path) -> None:
    """OSError 被静默跳过，不进入 sources。

    通过 mock Path.read_text 在 _read_if_exists 内部抛 OSError，
    验证该 OSError 被内部 catch 后返回 None（跳过该 source）。
    """
    existing = tmp_path / "unreadable.md"
    existing.write_text("some content", encoding="utf-8")

    # patch Path.read_text 使其抛 OSError，模拟权限不足场景
    with patch.object(Path, "read_text", side_effect=OSError("permission denied")):
        loader = InstructionLoader(extra_files=[existing], include_env=False)
        sources = _run(loader.load(None))

    assert sources == []


@pytest.mark.unit
def test_unicode_decode_error_bubbles(tmp_path: Path) -> None:
    """非法 UTF-8 文件导致 UnicodeDecodeError 冒泡（v0.1.2 正式契约）。"""
    bad_file = tmp_path / "bad_encoding.md"
    # 写入非合法 UTF-8 的字节序列
    bad_file.write_bytes(bytes([0x80, 0x81, 0x82]))

    loader = InstructionLoader(extra_files=[bad_file], include_env=False)
    with pytest.raises(UnicodeDecodeError):
        _run(loader.load(None))


# ---------------------------------------------------------------------------
# #4 render() 规则
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_render_includes_origin_prefix() -> None:
    """渲染后包含 '# agent_spec'、'# file:xxx' 等前缀。"""
    sources = [
        InstructionSource(origin="agent_spec", content="Rule A"),
        InstructionSource(origin="file:rules.md", content="Rule B"),
    ]
    result = InstructionLoader.render(sources)
    assert "# agent_spec" in result
    assert "# file:rules.md" in result


@pytest.mark.unit
def test_render_separates_sources_with_blank_line() -> None:
    """来源间用 '\\n\\n' 分隔（空行）。"""
    sources = [
        InstructionSource(origin="agent_spec", content="Part 1"),
        InstructionSource(origin="file:extra.md", content="Part 2"),
    ]
    result = InstructionLoader.render(sources)
    # 两个 part 之间必须有空行分隔
    assert "\n\n" in result
    parts = result.split("\n\n")
    assert len(parts) == 2


@pytest.mark.unit
def test_render_empty_content_source_excluded() -> None:
    """空 content 的来源不进入 render 结果。"""
    sources = [
        InstructionSource(origin="agent_spec", content=""),
        InstructionSource(origin="file:rules.md", content="Real content"),
    ]
    result = InstructionLoader.render(sources)
    assert "# agent_spec" not in result
    assert "# file:rules.md" in result


@pytest.mark.unit
def test_render_whitespace_only_content_excluded() -> None:
    """全空白 content 的来源不进入 render 结果（strip 后为空）。"""
    sources = [
        InstructionSource(origin="agent_spec", content="   \n  "),
        InstructionSource(origin="env:KONGMING_EXTRA_INSTRUCTIONS", content="Env content"),
    ]
    result = InstructionLoader.render(sources)
    assert "# agent_spec" not in result
    assert "# env:KONGMING_EXTRA_INSTRUCTIONS" in result


@pytest.mark.unit
def test_render_empty_sources_returns_empty_string() -> None:
    """空 sources 列表返回空字符串。"""
    result = InstructionLoader.render([])
    assert result == ""


@pytest.mark.unit
def test_render_source_order_stable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """来源顺序稳定：agent_spec → extra_files → env。"""
    rule_file = tmp_path / "rules.md"
    rule_file.write_text("File rules", encoding="utf-8")
    monkeypatch.setenv("KONGMING_EXTRA_INSTRUCTIONS", "Env rules")

    loader = InstructionLoader(extra_files=[rule_file], include_env=True)
    sources = _run(loader.load("Spec rules"))

    assert sources[0].origin == "agent_spec"
    assert sources[1].origin == "file:rules.md"
    assert sources[2].origin == "env:KONGMING_EXTRA_INSTRUCTIONS"

    result = InstructionLoader.render(sources)
    pos_spec = result.index("# agent_spec")
    pos_file = result.index("# file:rules.md")
    pos_env = result.index("# env:KONGMING_EXTRA_INSTRUCTIONS")
    assert pos_spec < pos_file < pos_env


@pytest.mark.unit
def test_render_format_is_hash_origin_newline_content() -> None:
    """每条来源格式为 '# {origin}\\n{content}'。"""
    sources = [InstructionSource(origin="agent_spec", content="Do the thing")]
    result = InstructionLoader.render(sources)
    assert result == "# agent_spec\nDo the thing"


# ---------------------------------------------------------------------------
# #5 同名文件 origin 冲突
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_same_filename_different_paths_both_kept(tmp_path: Path) -> None:
    """两个不同路径但同名文件 → sources 包含两条 origin='file:rules.md' 相同的条目。

    锁死已知限制：当前实现不去重不报错，两条 origin 相同的 source 都被保留。
    """
    dir_a = tmp_path / "dir_a"
    dir_b = tmp_path / "dir_b"
    dir_a.mkdir()
    dir_b.mkdir()

    rules_a = dir_a / "rules.md"
    rules_b = dir_b / "rules.md"
    rules_a.write_text("Rules from dir_a", encoding="utf-8")
    rules_b.write_text("Rules from dir_b", encoding="utf-8")

    loader = InstructionLoader(extra_files=[rules_a, rules_b], include_env=False)
    sources = _run(loader.load(None))

    # 两条 origin 相同的 source 都被保留
    file_sources = [s for s in sources if s.origin == "file:rules.md"]
    assert len(file_sources) == 2, (
        "已知限制：同名文件不去重，两条 origin='file:rules.md' 的 source 都应保留"
    )
    # 内容来自不同文件
    contents = {s.content for s in file_sources}
    assert "Rules from dir_a" in contents
    assert "Rules from dir_b" in contents
