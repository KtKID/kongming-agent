"""unit：context.prompts_loader 装配器覆盖。

模块 3 / task prompt-modules-v0.1.3。覆盖启动物化、HTML 注释剔除、
空白段跳过、用户编辑保留、异常冒泡等全部分支。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from context.prompts_loader import (
    TEMPLATE_FILENAMES,
    _strip_html_comments,
    materialize_and_load_prompts,
)

# ---------------------------------------------------------------------------
# 物化行为
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_first_run_materializes_all_three(tmp_path: Path) -> None:
    """首次调用后 home/prompts/ 下三文件都存在，内容等于内置模板。"""
    await materialize_and_load_prompts(tmp_path)

    prompts_dir = tmp_path / "prompts"
    assert prompts_dir.is_dir()
    for name in TEMPLATE_FILENAMES:
        target = prompts_dir / name
        assert target.is_file(), f"模板 {name} 应被物化到 {target}"
        # 物化出来的内容应与内置模板的内容一致（非空）
        assert target.read_text(encoding="utf-8")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_second_run_does_not_overwrite_user_edits(tmp_path: Path) -> None:
    """第一次调用后，修改 AGENT.md → 再次调用，用户编辑不被覆盖。"""
    await materialize_and_load_prompts(tmp_path)

    agent_md = tmp_path / "prompts" / "AGENT.md"
    user_edit = "用户自定义 agent 身份"
    agent_md.write_text(user_edit, encoding="utf-8")

    await materialize_and_load_prompts(tmp_path)

    assert agent_md.read_text(encoding="utf-8") == user_edit


@pytest.mark.unit
@pytest.mark.asyncio
async def test_partial_missing_only_materializes_missing_ones(tmp_path: Path) -> None:
    """home/prompts 已存在 + 只预置 AGENT.md → TOOLS/USER 被物化，AGENT.md 不动。"""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()

    custom_agent = "我已经写好了自己的 agent 描述。"
    (prompts_dir / "AGENT.md").write_text(custom_agent, encoding="utf-8")

    await materialize_and_load_prompts(tmp_path)

    assert (prompts_dir / "AGENT.md").read_text(encoding="utf-8") == custom_agent
    assert (prompts_dir / "TOOLS.md").is_file()
    assert (prompts_dir / "USER.md").is_file()
    # 模板本身非空
    assert (prompts_dir / "TOOLS.md").read_text(encoding="utf-8")
    assert (prompts_dir / "USER.md").read_text(encoding="utf-8")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_creates_prompts_dir_when_missing(tmp_path: Path) -> None:
    """home 下没有 prompts 子目录 → 调用后被创建。"""
    prompts_dir = tmp_path / "prompts"
    assert not prompts_dir.exists()

    await materialize_and_load_prompts(tmp_path)

    assert prompts_dir.is_dir()


# ---------------------------------------------------------------------------
# 装配返回值
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_returns_combined_three_sections_by_default(tmp_path: Path) -> None:
    """首次装配返回值包含三部分内容，用 '\\n\\n' 连接。"""
    result = await materialize_and_load_prompts(tmp_path)

    assert "你是 kongming-agent" in result
    assert "长期记忆" in result
    assert "测试占位" in result
    # 至少存在两个 "\n\n" 分节符（三段之间两个空行）
    assert result.count("\n\n") >= 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_user_md_html_comments_are_stripped(tmp_path: Path) -> None:
    """返回值里不含 HTML 注释字样（因为 USER.md 默认包含注释块）。"""
    result = await materialize_and_load_prompts(tmp_path)

    assert "<!--" not in result
    assert "-->" not in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_user_md_cleaned_empty_skipped(tmp_path: Path) -> None:
    """USER.md 清空 → 返回值不含测试占位文字，且不以 '\\n\\n' 结尾。"""
    # 先物化一次，再把 USER.md 清空
    await materialize_and_load_prompts(tmp_path)
    (tmp_path / "prompts" / "USER.md").write_text("", encoding="utf-8")

    result = await materialize_and_load_prompts(tmp_path)

    assert "测试占位" not in result
    assert not result.endswith("\n\n")
    # AGENT + TOOLS 两段应仍在
    assert "你是 kongming-agent" in result
    assert "长期记忆" in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_user_md_fully_removed_skipped(tmp_path: Path) -> None:
    """USER.md 仅含 HTML 注释 → strip 后为空 → 不拼入。"""
    await materialize_and_load_prompts(tmp_path)
    (tmp_path / "prompts" / "USER.md").write_text("<!-- only comment -->", encoding="utf-8")

    result = await materialize_and_load_prompts(tmp_path)

    assert "测试占位" not in result
    assert "<!--" not in result
    assert not result.endswith("\n\n")
    # AGENT + TOOLS 两段应仍在
    assert "你是 kongming-agent" in result
    assert "长期记忆" in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_three_empty_returns_empty_string(tmp_path: Path) -> None:
    """三文件都只含注释 → 返回 ''。"""
    await materialize_and_load_prompts(tmp_path)

    prompts_dir = tmp_path / "prompts"
    for name in TEMPLATE_FILENAMES:
        (prompts_dir / name).write_text("<!-- empty -->", encoding="utf-8")

    result = await materialize_and_load_prompts(tmp_path)

    assert result == ""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_user_edits_agent_reflected_in_output(tmp_path: Path) -> None:
    """用户把 AGENT.md 改成自定义内容 → 返回值包含自定义内容，不含默认。"""
    await materialize_and_load_prompts(tmp_path)

    custom = "Custom agent identity"
    (tmp_path / "prompts" / "AGENT.md").write_text(custom, encoding="utf-8")

    result = await materialize_and_load_prompts(tmp_path)

    assert custom in result
    assert "你是 kongming-agent" not in result


# ---------------------------------------------------------------------------
# 辅助函数 _strip_html_comments
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_strip_html_comments_single_line() -> None:
    """单行注释被整体剔除。"""
    assert _strip_html_comments("<!-- x -->") == ""


@pytest.mark.unit
def test_strip_html_comments_multi_line() -> None:
    """跨行注释完整剔除。"""
    text = "<!--\nmulti\nline\ncomment\n-->"
    assert _strip_html_comments(text) == ""


@pytest.mark.unit
def test_strip_html_comments_multiple_blocks() -> None:
    """多个独立注释块都被剔除。"""
    text = "<!-- a -->keep1<!-- b -->keep2<!-- c -->"
    result = _strip_html_comments(text)
    assert "<!--" not in result
    assert "-->" not in result
    assert "keep1" in result
    assert "keep2" in result


@pytest.mark.unit
def test_strip_html_comments_non_greedy() -> None:
    """非贪婪匹配：'<!-- a -->middle<!-- b -->' → 保留 'middle'。"""
    assert _strip_html_comments("<!-- a -->middle<!-- b -->") == "middle"


@pytest.mark.unit
def test_strip_html_comments_empty_input() -> None:
    """空字符串 → 空字符串。"""
    assert _strip_html_comments("") == ""


# ---------------------------------------------------------------------------
# 异常分支
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_utf8_user_md_raises_unicode_error(tmp_path: Path) -> None:
    """USER.md 存入非 UTF-8 字节 → 装配时 UnicodeDecodeError 冒泡。"""
    await materialize_and_load_prompts(tmp_path)
    # 非法 UTF-8 字节序列
    (tmp_path / "prompts" / "USER.md").write_bytes(bytes([0x80, 0x81, 0x82]))

    with pytest.raises(UnicodeDecodeError):
        await materialize_and_load_prompts(tmp_path)
