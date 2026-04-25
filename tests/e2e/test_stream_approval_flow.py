"""E#3：流式下 tool_call + approval 交互。

覆盖：
- 流式 tool_call → approval 通过 → 工具执行 → 第二轮 message.done → 完成
- approval 拒绝路径
- 流式 tool_call 时 content suppress（runner 层决策，不进 EventSink）
"""

from __future__ import annotations

from typing import Any

import pytest

from core import AgentSpec, InMemorySession, Runner
from core.contracts import LLMStreamChunk, ToolContext, ToolResult
from core.message import Message, ToolCall
from tests.e2e.conftest import MemoryEventSink, RecordingApproval, StubLLMStreamProvider


class StubTool:
    """最小 Tool 实现：execute 返回固定 ToolResult。"""

    name = "echo_tool"
    description = "echo args"
    input_schema: dict = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        self.calls.append(args)
        return ToolResult(ok=True, content="echo-result")


def _spec_with_tool() -> AgentSpec:
    return AgentSpec(
        name="t", instructions="x", default_model="m1", max_turns=5,
        tool_names=("echo_tool",),
    )


@pytest.mark.e2e
async def test_e3_1_stream_tool_call_approved_then_complete() -> None:
    """E.3.1：流式 tool_call → approval 通过 → 工具执行 → 第二轮终态。"""
    tc = ToolCall(call_id="c1", tool_name="echo_tool", arguments={"x": 1})
    # 第一轮：tool_call
    turn1 = [
        LLMStreamChunk(kind="tool_call.start", index=0, tool_call_id="c1", tool_name="echo_tool"),
        LLMStreamChunk(kind="tool_call.end", index=0, tool_call_id="c1", tool_name="echo_tool"),
        LLMStreamChunk(
            kind="message.done",
            message=Message.assistant(content=None, tool_calls=[tc]),
            finish_reason="tool_calls",
        ),
    ]
    # 第二轮：基于 tool 结果的最终回复
    turn2 = [
        LLMStreamChunk(kind="content.delta", delta="done", index=0),
        LLMStreamChunk(
            kind="message.done", message=Message.assistant(content="done"), finish_reason="stop"
        ),
    ]

    stub = StubLLMStreamProvider()
    stub.script_chunks(turn1)
    stub.script_chunks(turn2)

    tool = StubTool()
    sink = MemoryEventSink()
    approval = RecordingApproval(outcome="approved")

    runner = Runner(stream_enabled=True, event_sinks=[sink])
    res = await runner.run(
        "use tool", session=InMemorySession("appr1"), agent_spec=_spec_with_tool(),
        llm=stub, tools={"echo_tool": tool}, approval=approval,
    )

    assert res.status == "completed"
    assert res.final_message.content == "done"
    # 工具被调用一次，参数正确
    assert len(tool.calls) == 1
    assert tool.calls[0] == {"x": 1}
    # approval 被请求一次
    assert len(approval.requests) == 1


@pytest.mark.e2e
async def test_e3_2_stream_tool_call_rejected() -> None:
    """E.3.2：approval 拒绝时流式 tool_call 不执行，runner 仍能继续到下一轮。"""
    tc = ToolCall(call_id="c1", tool_name="echo_tool", arguments={})
    turn1 = [
        LLMStreamChunk(kind="tool_call.start", index=0, tool_call_id="c1", tool_name="echo_tool"),
        LLMStreamChunk(kind="tool_call.end", index=0, tool_call_id="c1", tool_name="echo_tool"),
        LLMStreamChunk(
            kind="message.done",
            message=Message.assistant(content=None, tool_calls=[tc]),
            finish_reason="tool_calls",
        ),
    ]
    # 第二轮：拿到 approval rejected 后给一个收尾
    turn2 = [
        LLMStreamChunk(
            kind="message.done",
            message=Message.assistant(content="rejected, abort"),
            finish_reason="stop",
        ),
    ]

    stub = StubLLMStreamProvider()
    stub.script_chunks(turn1)
    stub.script_chunks(turn2)

    tool = StubTool()
    approval = RecordingApproval(outcome="rejected")

    runner = Runner(stream_enabled=True)
    res = await runner.run(
        "use tool", session=InMemorySession("appr2"), agent_spec=_spec_with_tool(),
        llm=stub, tools={"echo_tool": tool}, approval=approval,
    )

    # tool 没被执行
    assert len(tool.calls) == 0
    # approval 被询问
    assert len(approval.requests) == 1
    # runner 仍走第二轮，最终完成
    assert res.status == "completed"


@pytest.mark.e2e
async def test_e3_3_suppress_content_after_tool_call() -> None:
    """E.3.3：tool_call 后的 content.delta 被 runner 屏蔽（suppress=True 默认）。"""
    tc = ToolCall(call_id="c1", tool_name="echo_tool", arguments={})
    turn1 = [
        LLMStreamChunk(kind="content.delta", delta="pre-call", index=0),
        LLMStreamChunk(kind="tool_call.start", index=0, tool_call_id="c1", tool_name="echo_tool"),
        LLMStreamChunk(kind="content.delta", delta="POST-CALL-LEAK", index=0),
        LLMStreamChunk(kind="tool_call.end", index=0, tool_call_id="c1", tool_name="echo_tool"),
        LLMStreamChunk(
            kind="message.done",
            message=Message.assistant(content="pre-call", tool_calls=[tc]),
            finish_reason="tool_calls",
        ),
    ]
    turn2 = [
        LLMStreamChunk(
            kind="message.done",
            message=Message.assistant(content="ok"),
            finish_reason="stop",
        ),
    ]

    stub = StubLLMStreamProvider()
    stub.script_chunks(turn1)
    stub.script_chunks(turn2)

    sink = MemoryEventSink()
    runner = Runner(stream_enabled=True, suppress_content_after_tool_call=True, event_sinks=[sink])
    await runner.run(
        "x", session=InMemorySession("appr3"), agent_spec=_spec_with_tool(),
        llm=stub, tools={"echo_tool": StubTool()}, approval=RecordingApproval(),
    )

    # 只有 pre-call 进 EventSink；POST-CALL-LEAK 被 runner 屏蔽
    content_deltas = [
        e.payload["delta"] for e in sink.of_kind("content.delta")
    ]
    assert "pre-call" in content_deltas
    assert "POST-CALL-LEAK" not in content_deltas
