"""unit：InstructionLoader 三类来源加载、缺失语义、render 规则契约。

覆盖范围（对应 dev-checklist.md #2 #3 #4 #5）：

#2 三类来源加载（agent_spec / extra_files / env）
#3 缺失语义（文件不存在/空文件/全空白/OSError 跳过，UnicodeDecodeError 冒泡）
#4 render() 规则（# origin 前缀、空行分隔、空来源过滤、顺序稳定）
#5 同名文件 origin 冲突（两条相同 origin 的 source 都被保留，锁死已知限制）
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import prompting.assembly.runtime_context as runtime_context_mod
from prompting.instructions.instruction_loader import (
    InstructionLoader,
    InstructionSource,
    assemble_instructions,
)

# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------


def _run(coro):  # type: ignore[no-untyped-def]
    """在测试中同步运行 async 函数。

    用 ``asyncio.run`` 替代 deprecated 的 ``get_event_loop().run_until_complete``：
    后者在 Python 3.11+ 下，若当前线程没有 running loop（如前面跑过
    ``@pytest.mark.asyncio`` 测试关闭了 loop），会抛 RuntimeError。
    ``asyncio.run`` 始终新建独立 loop，跨测隔离。

    (smart-approval-manager-stage1 加 42 个 async 测后，pytest --picked -n 4
    分组让 instruction_loader 测试跑在 async 测试之后，pre-push 暴露此 deprecated 问题)
    """
    return asyncio.run(coro)


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


# ---------------------------------------------------------------------------
# #6 assemble_instructions — sitian_root 回归 & 注入
# ---------------------------------------------------------------------------


def _patch_assemble_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock materialize_and_load_prompts 和 build_runtime_context_text，
    隔离 assemble_instructions 对文件系统的依赖。

    两个函数是在 assemble_instructions 内部 lazy import 的，
    因此 patch 目标是各自源模块。
    """
    monkeypatch.setattr(
        "prompting.instructions.prompts_loader.materialize_and_load_prompts",
        AsyncMock(return_value="base prompt"),
    )
    monkeypatch.setattr(
        runtime_context_mod, "build_runtime_context_text", lambda **_kw: "runtime ctx"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_assemble_sitian_root_none_no_sitian_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归：sitian_root=None 时，origins 不含 'sitian'。"""
    _patch_assemble_deps(monkeypatch)
    monkeypatch.delenv("KONGMING_EXTRA_INSTRUCTIONS", raising=False)

    _rendered, origins = await assemble_instructions(
        kongming_home=tmp_path,
        sitian_root=None,
    )
    assert "sitian" not in origins


@pytest.mark.unit
@pytest.mark.asyncio
async def test_assemble_pre_file_sources_before_prompt_files_env_and_sitian(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证早期 source 顺序，输入为 workflow source/file/env/sitian，输出 workflow 位于文件前。"""
    _patch_assemble_deps(monkeypatch)
    monkeypatch.setenv("KONGMING_EXTRA_INSTRUCTIONS", "env instructions")
    extra_file = tmp_path / "extra.md"
    extra_file.write_text("file instructions", encoding="utf-8")

    sitian_dir = tmp_path / "sitian"
    channel_dir = sitian_dir / "claude"
    channel_dir.mkdir(parents=True)
    (channel_dir / "workspace_state.json").write_text(
        json.dumps(
            {
                "updatedAt": "2026-05-10T08:00:00+00:00",
                "sources": {"total": 1, "active": 1},
                "workItems": [
                    {
                        "id": "wf",
                        "title": "workflow catalog",
                        "status": "active",
                        "priority": "high",
                        "updatedAt": "2026-05-10T07:55:00+00:00",
                        "nextActions": ["验证顺序"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rendered, origins = await assemble_instructions(
        kongming_home=tmp_path,
        extra_files=[extra_file],
        pre_file_sources=[
            InstructionSource(
                origin="workflow_catalog",
                content="# workflow catalog\nworkflow listing",
            )
        ],
        sitian_root=sitian_dir,
    )

    assert origins == [
        "runtime",
        "workflow_catalog",
        "agent_spec",
        "file:extra.md",
        "env:KONGMING_EXTRA_INSTRUCTIONS",
        "sitian",
    ]
    assert rendered.index("# workflow_catalog") < rendered.index("# agent_spec")
    assert rendered.index("# workflow_catalog") < rendered.index("# file:extra.md")
    assert rendered.index("# workflow_catalog") < rendered.index(
        "# env:KONGMING_EXTRA_INSTRUCTIONS"
    )
    assert rendered.index("# workflow_catalog") < rendered.index("# sitian")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_assemble_sitian_root_valid_injects_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注入：sitian_root 包含有效 workspace_state.json 时，origins 含 'sitian'，
    rendered 文本包含 '工作区态势'。"""
    _patch_assemble_deps(monkeypatch)
    monkeypatch.delenv("KONGMING_EXTRA_INSTRUCTIONS", raising=False)

    sitian_dir = tmp_path / "sitian"
    channel_dir = sitian_dir / "claude"
    channel_dir.mkdir(parents=True)
    state = {
        "updatedAt": "2026-05-10T08:00:00+00:00",
        "sources": {"total": 3, "active": 2},
        "workItems": [
            {
                "id": "item-1",
                "title": "测试任务",
                "status": "active",
                "priority": "high",
                "updatedAt": "2026-05-10T07:55:00+00:00",
                "nextActions": ["完成测试"],
            },
        ],
    }
    (channel_dir / "workspace_state.json").write_text(
        json.dumps(state, ensure_ascii=False),
        encoding="utf-8",
    )

    rendered, origins = await assemble_instructions(
        kongming_home=tmp_path,
        sitian_root=sitian_dir,
    )
    assert "sitian" in origins
    assert "工作区态势" in rendered


@pytest.mark.unit
@pytest.mark.asyncio
async def test_assemble_sitian_rendered_prompt_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快照：验证多频道 sitian 注入后完整 rendered prompt 的结构和内容。

    构造 sitian_root/claude/ 频道（3 个项目 + blocker + risk + summary），
    断言 rendered 文本中 # sitian 段包含频道标题、表格、阻塞项、风险项、摘要。
    """
    _patch_assemble_deps(monkeypatch)
    monkeypatch.delenv("KONGMING_EXTRA_INSTRUCTIONS", raising=False)

    sitian_root = tmp_path / "sitian"
    sitian_root.mkdir()
    claude_dir = sitian_root / "claude"
    claude_dir.mkdir()
    state = {
        "updatedAt": "2026-05-10T08:00:00+00:00",
        "sources": {"total": 3, "active": 2},
        "workItems": [
            {
                "id": "proj-a",
                "title": "kongming-agent",
                "status": "active",
                "priority": "low",
                "updatedAt": "2026-05-10T07:55:00+00:00",
                "nextActions": ["查看最新线程"],
            },
            {
                "id": "proj-b",
                "title": "x-memo",
                "status": "waiting",
                "priority": "medium",
                "updatedAt": "2026-05-09T10:00:00+00:00",
                "nextActions": ["补充进展"],
            },
            {
                "id": "proj-c",
                "title": "ralph",
                "status": "blocked",
                "priority": "high",
                "updatedAt": "2026-05-05T06:00:00+00:00",
                "nextActions": [],
            },
        ],
        "blockers": [
            {"workItemId": "proj-c", "summary": "扫描路径不存在", "severity": "high"},
        ],
        "risks": [
            {"category": "stale_activity", "summary": "ralph 5 天无进展"},
        ],
    }
    (claude_dir / "workspace_state.json").write_text(
        json.dumps(state, ensure_ascii=False),
        encoding="utf-8",
    )
    (claude_dir / "latest_summary.md").write_text(
        "# SiTian Summary\n\n- Sources: 2/3 active\n",
        encoding="utf-8",
    )

    rendered, origins = await assemble_instructions(
        kongming_home=tmp_path,
        sitian_root=sitian_root,
    )
    assert "sitian" in origins

    sitian_section = rendered.split("# sitian\n", 1)[1]

    assert "工作区态势（司天观察）" in sitian_section
    assert "频道数：1" in sitian_section
    assert "### 频道：claude" in sitian_section
    assert "数据采集于 2026-05-10T08:00:00+00:00" in sitian_section
    assert "2/3 活跃，3 个项目" in sitian_section

    assert "| 1 | kongming-agent | 活跃 | low |" in sitian_section
    assert "| 2 | x-memo | 等待中 | medium |" in sitian_section
    assert "| 3 | ralph | 阻塞 | high |" in sitian_section
    assert "查看最新线程" in sitian_section
    assert "补充进展" in sitian_section
    assert "同步最新状态" in sitian_section

    assert "#### 阻塞项" in sitian_section
    assert "proj-c: 扫描路径不存在" in sitian_section

    assert "#### 风险项" in sitian_section
    assert "stale_activity: ralph 5 天无进展" in sitian_section

    assert "#### 最近摘要" in sitian_section
    assert "SiTian Summary" in sitian_section

    assert "询问用户手动触发扫描" in sitian_section


@pytest.mark.asyncio
async def test_assemble_sitian_real_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """用真实 sitian 产物目录验证多频道注入效果。

    通过 SITIAN_TEST_ROOT 环境变量指定频道父目录（如 /Users/kid/.SiTian）。
    未设置时跳过。只做结构性断言，不写死具体项目名。
    """
    import os

    sitian_dir_str = os.environ.get("SITIAN_TEST_ROOT")
    if not sitian_dir_str:
        pytest.skip("SITIAN_TEST_ROOT not set")
    sitian_dir = Path(sitian_dir_str)
    if not sitian_dir.is_dir():
        pytest.skip(f"directory not found: {sitian_dir}")
    has_channel = any(
        (d / "workspace_state.json").exists() for d in sitian_dir.iterdir() if d.is_dir()
    )
    if not has_channel:
        pytest.skip(f"no channel with workspace_state.json under {sitian_dir}")

    _patch_assemble_deps(monkeypatch)
    monkeypatch.delenv("KONGMING_EXTRA_INSTRUCTIONS", raising=False)

    rendered, origins = await assemble_instructions(
        kongming_home=tmp_path,
        sitian_root=sitian_dir,
    )
    assert "sitian" in origins

    assert "# sitian\n" in rendered
    sitian_section = rendered.split("# sitian\n", 1)[1]

    assert "工作区态势（司天观察）" in sitian_section
    assert "频道数：" in sitian_section
    assert "### 频道：" in sitian_section
    assert "| # | 项目 | 状态 | 优先级 | 最近活跃 | 下一步 |" in sitian_section
    assert "| 1 |" in sitian_section
    assert "询问用户手动触发扫描" in sitian_section

    print("\n===== sitian prompt section =====")
    print(sitian_section)
    print("===== end =====")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_assemble_sitian_root_empty_dir_no_sitian_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """降级：sitian_root 存在但无 workspace_state.json 时，origins 不含 'sitian'。"""
    _patch_assemble_deps(monkeypatch)
    monkeypatch.delenv("KONGMING_EXTRA_INSTRUCTIONS", raising=False)

    sitian_dir = tmp_path / "sitian_empty"
    sitian_dir.mkdir()

    _rendered, origins = await assemble_instructions(
        kongming_home=tmp_path,
        sitian_root=sitian_dir,
    )
    assert "sitian" not in origins
