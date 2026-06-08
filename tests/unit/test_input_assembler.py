"""unit：InputAssembler prompt 组装契约。

覆盖范围（对应 dev-checklist.md #1 #2）：

#1 legacy/imported history 已有 system 时，assemble 不追加第二条 system 消息
#2 compactor 删掉已有 system 时，同轮 assemble 不重补（避免震荡）
#3 结构化 sources 与预渲染 prompt 透传

每个测试完成后将 assembled 结果落盘到 ``.kongming/debug/tests/assembler-<timestamp>.json``。
落到 ``debug/tests/`` 子目录是为了和 CLI ``--debug`` 生产落盘的
``.kongming/debug/prompt-debug-*.json`` 物理分开，方便人工查看。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.message import Message
from prompting.input_assembler import InputAssembler
from prompting.instruction_loader import InstructionSource

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DUMP_DIR = _REPO_ROOT / ".kongming/debug/tests"


def _dump_assemble_result(test_name: str, result: object, sources: list[InstructionSource]) -> None:
    """把 assemble 结果落盘为时间戳命名的 JSON 文件。"""
    from prompting.input_assembler import AssembledInput

    assert isinstance(result, AssembledInput)
    _DUMP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")

    messages_dicts: list[dict] = []
    for m in result.messages:
        d: dict = {"role": m.role, "content": m.content}
        if m.tool_calls:
            d["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in m.tool_calls
            ]
        messages_dicts.append(d)

    dump = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "test": test_name,
        "instruction_sources": [s.origin for s in sources],
        "metadata": result.metadata,
        "assembled_messages": messages_dicts,
    }

    path = _DUMP_DIR / f"assembler-{test_name}-{ts}.json"
    path.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n📄 Assembler dump: {path.resolve()}")


# ---------------------------------------------------------------------------
# #1 legacy/imported history：history 已有 system → 不重追加
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_legacy_history_with_existing_system_not_duplicated() -> None:
    """legacy/imported history 已有 system 时，assemble 只保留原 system。"""
    existing_system = Message.system("You are helpful.")
    history = [existing_system, Message.user("Hello"), Message.assistant("Hi")]
    sources = [InstructionSource(origin="agent_spec", content="You are helpful.")]

    assembler = InputAssembler()
    result = await assembler.assemble(history, sources)

    system_msgs = [m for m in result.messages if m.role == "system"]
    assert len(system_msgs) == 1, "history 里已有 system，不应追加第二条"
    assert result.metadata["added_system"] is False
    _dump_assemble_result("legacy-existing-system-not-duplicated", result, sources)


@pytest.mark.unit
async def test_no_history_no_sources_produces_no_system() -> None:
    """空 history + 空 sources → 无 system 消息。"""
    assembler = InputAssembler()
    result = await assembler.assemble([], [])

    system_msgs = [m for m in result.messages if m.role == "system"]
    assert len(system_msgs) == 0
    assert result.metadata["added_system"] is False
    _dump_assemble_result("no-history-no-sources", result, [])


@pytest.mark.unit
async def test_empty_history_with_sources_adds_system() -> None:
    """空 history + 有 sources → 追加 system 消息。"""
    sources = [InstructionSource(origin="agent_spec", content="Be precise.")]
    assembler = InputAssembler()
    result = await assembler.assemble([], sources)

    system_msgs = [m for m in result.messages if m.role == "system"]
    assert len(system_msgs) == 1
    assert result.metadata["added_system"] is True
    assert "agent_spec" in result.metadata["instruction_sources"]
    _dump_assemble_result("empty-history-with-sources", result, sources)


@pytest.mark.unit
async def test_pre_rendered_source_passthrough_exact_content() -> None:
    """生产链路传入 origin="" 的预渲染 prompt 时，assembler 精确透传内容。"""
    rendered_prompt = "# agent_spec\nYou are kongming agent.\n\n# memory\nRemember concise replies."
    sources = [InstructionSource(origin="", content=rendered_prompt)]
    assembler = InputAssembler()
    result = await assembler.assemble([], sources)

    system_msgs = [m for m in result.messages if m.role == "system"]
    assert len(system_msgs) == 1
    assert system_msgs[0].content == rendered_prompt
    assert result.metadata["instruction_sources"] == [""]
    assert result.metadata["added_system"] is True
    _dump_assemble_result("pre-rendered-source-passthrough", result, sources)


# ---------------------------------------------------------------------------
# #2 compactor 删掉已有 system → 同轮不重补
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_compact_removes_system_no_readd() -> None:
    """compactor 删掉已有 system 时，同轮 assemble 不重补（避免震荡）。

    InputAssembler 在原始 history 上判断 has_existing_system，然后才做 compact。
    若 compact 结果删除了 system，assembler 判定"已有 system"→ 不追加新的，
    最终 messages 里没有任何 system 消息。
    """
    existing_system = Message.system("Old system prompt.")
    history = [existing_system, Message.user("Hello")]
    sources = [InstructionSource(origin="agent_spec", content="Instructions")]

    # Mock compactor: 把 system 消息从结果里去掉
    mock_compactor = MagicMock()
    mock_compactor.compact = AsyncMock(return_value=[m for m in history if m.role != "system"])

    assembler = InputAssembler(compactor=mock_compactor)  # type: ignore[arg-type]
    result = await assembler.assemble(history, sources)

    # compact 删掉 system，assembler 看到原始 history 有 system → 不重补
    system_msgs = [m for m in result.messages if m.role == "system"]
    assert len(system_msgs) == 0, "compact 删掉 system 后，同轮不重补"
    assert result.metadata["added_system"] is False
    _dump_assemble_result("compact-removes-system-no-readd", result, sources)


# ---------------------------------------------------------------------------
# #3 结构化 sources 渲染
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_structured_memory_source_rendered_into_system() -> None:
    """直接传结构化 sources 时，assembler 用 InstructionLoader.render() 组合内容。"""
    memory_content = "## 用户偏好\nUse concise answers."
    sources = [
        InstructionSource(origin="agent_spec", content="You are helpful."),
        InstructionSource(origin="memory", content=memory_content),
    ]
    assembler = InputAssembler()
    result = await assembler.assemble([], sources)

    system_msgs = [m for m in result.messages if m.role == "system"]
    assert len(system_msgs) == 1
    assert result.metadata["added_system"] is True

    # metadata 记录了所有 instruction sources
    assert "agent_spec" in result.metadata["instruction_sources"]
    assert "memory" in result.metadata["instruction_sources"]

    # system 消息内容包含两个来源
    system_text = system_msgs[0].content
    assert "# agent_spec" in system_text
    assert "# memory" in system_text
    assert "You are helpful" in system_text
    assert memory_content.strip() in system_text
    _dump_assemble_result("structured-memory-source-rendered", result, sources)


@pytest.mark.unit
async def test_memory_source_only_injects_when_content_present() -> None:
    """memory 来源内容为空时，不产生 system 消息。"""
    sources = [InstructionSource(origin="memory", content="")]
    assembler = InputAssembler()
    result = await assembler.assemble([], sources)

    # 空 content 被 render() 跳过，等同于无 sources
    system_msgs = [m for m in result.messages if m.role == "system"]
    assert len(system_msgs) == 0
    assert result.metadata["added_system"] is False
    _dump_assemble_result("memory-source-empty", result, sources)
