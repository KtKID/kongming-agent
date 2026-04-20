"""e2e P0-2：tool call 主链路。

覆盖 plan.md "e2e 主链路基线"的第二条：

> 模型发出一个最小 file 或 shell tool call，系统完成工具执行、结果回填和最终收口。

用 :class:`core.Runner` 直接装配 + ``StubLLMProvider`` + 真实 ``ReadFileTool``，
验证："模型第一轮发 tool call → approval 放行 → tool 执行 → 结果回填 → 模型
第二轮给终止文本"这条闭环。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import AgentSpec, InMemorySession, Runner, ToolCall
from tests.e2e.conftest import MemoryEventSink, RecordingApproval, StubLLMProvider
from tools import ReadFileTool, WriteFileTool


@pytest.mark.e2e
async def test_tool_call_read_file_executes_and_feeds_back(
    stub_llm: StubLLMProvider,
    memory_sink: MemoryEventSink,
    recording_approval: RecordingApproval,
    tmp_path: Path,
) -> None:
    """读文件工具调用的完整闭环。"""
    target = tmp_path / "hello.txt"
    target.write_text("world")

    # 第 1 轮：模型发 read_file tool call。
    stub_llm.script(
        tool_calls=[
            ToolCall(
                call_id="c1",
                tool_name="read_file",
                arguments={"path": str(target)},
            )
        ],
    )
    # 第 2 轮：模型看到 tool result 后给出最终文本，终止。
    stub_llm.script(content="file content is: world")

    tool = ReadFileTool()
    registry: dict[str, object] = {"read_file": tool}

    runner = Runner(event_sinks=[memory_sink])
    session = InMemorySession("test-tool")
    spec = AgentSpec(
        name="t",
        instructions="You read files.",
        default_model="stub",
        tool_names=("read_file",),
        max_turns=5,
    )

    result = await runner.run(
        "请读一下 hello.txt",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools=registry,
        approval=recording_approval,
    )

    assert result.status == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "file content is: world"
    # 至少走了 2 轮（tool call + final）
    assert result.turn_count >= 2

    # approval 应该被问过一次
    assert len(recording_approval.requests) == 1
    assert recording_approval.requests[0].tool_name == "read_file"
    assert recording_approval.requests[0].arguments == {"path": str(target)}

    # 事件流覆盖 tool.call.start / tool.call.end / approval.request / approval.decision
    kinds = memory_sink.kinds()
    assert "tool.call.start" in kinds
    assert "tool.call.end" in kinds
    assert "approval.request" in kinds
    assert "approval.decision" in kinds

    # tool.call.end 的 payload ok=True
    end_events = memory_sink.of_kind("tool.call.end")
    assert any(e.payload.get("ok") is True for e in end_events)

    # session 历史里应该有一条 tool 角色的回填消息，content 包含 "world"
    history = await session.history()
    tool_msgs = [m for m in history if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "c1"
    assert "world" in (tool_msgs[0].content or "")


@pytest.mark.e2e
async def test_tool_call_write_file_executes_and_creates_file(
    stub_llm: StubLLMProvider,
    recording_approval: RecordingApproval,
    tmp_path: Path,
) -> None:
    """写文件工具调用也能走通闭环，并真的创建出文件。"""
    target = tmp_path / "out.txt"

    stub_llm.script(
        tool_calls=[
            ToolCall(
                call_id="w1",
                tool_name="write_file",
                arguments={"path": str(target), "content": "hi there"},
            )
        ],
    )
    stub_llm.script(content="wrote ok")

    tool = WriteFileTool()
    registry: dict[str, object] = {"write_file": tool}

    runner = Runner()
    session = InMemorySession("test-write")
    spec = AgentSpec(
        name="t",
        instructions="You write files.",
        default_model="stub",
        tool_names=("write_file",),
        max_turns=5,
    )

    result = await runner.run(
        "写一个文件",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools=registry,
        approval=recording_approval,
    )

    assert result.status == "completed"
    assert target.exists()
    assert target.read_text() == "hi there"


@pytest.mark.e2e
async def test_tool_call_missing_tool_is_reported_as_error_message(
    stub_llm: StubLLMProvider,
    recording_approval: RecordingApproval,
) -> None:
    """模型调了一个没注册的 tool 时，runner 应该写一条 tool error 消息并继续。"""
    stub_llm.script(
        tool_calls=[
            ToolCall(
                call_id="x1",
                tool_name="nonexistent_tool",
                arguments={},
            )
        ],
    )
    stub_llm.script(content="recovered")

    runner = Runner()
    session = InMemorySession("test-missing")
    # 空 registry + enabled_tools 也空，避免 runner 在 _resolve_tools 抛错
    spec = AgentSpec(
        name="t",
        instructions="",
        default_model="stub",
        tool_names=(),
        max_turns=5,
    )

    result = await runner.run(
        "call something",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )

    # 仍然 completed，最终模型返回 'recovered'
    assert result.status == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "recovered"

    # 历史里有一条 tool 角色的错误消息
    history = await session.history()
    tool_msgs = [m for m in history if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "not registered" in (tool_msgs[0].content or "")
