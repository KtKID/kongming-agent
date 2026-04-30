"""unit：runner emit ``tool.call.end`` 事件 payload 必须含 ToolResult 4 字段。

v0.1.6 修 web UI"工具结果显示空 {}"bug 的回归保护：之前 runner emit
``tool.call.end`` 时 payload 只放 ``call_id / tool_name / ok``，导致下游
trace.jsonl 与 web frame 都拿不到 ``content`` / ``data`` / ``error_message``。
本测试覆盖 3 条 emit 路径：

1. 正常工具执行成功 → payload 含 content / data / error_message
2. 工具未注册（unknown tool）→ payload 含 error_message
3. 审批拒绝 → payload 含 error_message
"""

from __future__ import annotations

from typing import Any

import pytest

from core import AgentSpec, InMemorySession, Runner, ToolCall
from core.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    Event,
    LLMRequest,
    LLMResponse,
    ToolContext,
    ToolResult,
)
from core.message import Message


class _StubLLM:
    def __init__(
        self,
        responses: list[tuple[str | None, list[ToolCall] | None]],
    ) -> None:
        self._responses = list(responses)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if not self._responses:
            return LLMResponse(message=Message(role="assistant", content=""), finish_reason="stop")
        content, tool_calls = self._responses.pop(0)
        calls_tuple = tuple(tool_calls) if tool_calls else None
        msg = Message(role="assistant", content=content, tool_calls=calls_tuple)
        finish = "tool_calls" if calls_tuple else "stop"
        return LLMResponse(message=msg, finish_reason=finish)


class _AllowApproval:
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(outcome="approved")


class _DenyApproval:
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(outcome="rejected", reason="not allowed")


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


class _RichResultTool:
    """返回带 content + data 的 ToolResult，验证两字段都通过 emit 传出。"""

    name = "rich"
    description = "rich"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(
            ok=True,
            content="stdout text",
            data={"exit_code": 0, "lines": 2},
        )


def _find_tool_call_end_events(sink: _RecordingSink) -> list[Event]:
    return [e for e in sink.events if e.kind == "tool.call.end"]


@pytest.mark.asyncio
async def test_tool_call_end_payload_carries_content_and_data() -> None:
    """正常路径：runner 把 ToolResult.content / data / error_message 写进 payload。"""
    llm = _StubLLM(
        [
            (None, [ToolCall(call_id="c1", tool_name="rich", arguments={})]),
            ("done", None),
        ]
    )
    sink = _RecordingSink()
    runner = Runner(event_sinks=[sink])
    await runner.run(
        "hi",
        session=InMemorySession("s"),
        agent_spec=AgentSpec(
            name="t",
            instructions="",
            default_model="m",
            tool_names=("rich",),
            max_turns=5,
        ),
        llm=llm,
        tools={"rich": _RichResultTool()},
        approval=_AllowApproval(),
    )

    ends = _find_tool_call_end_events(sink)
    assert len(ends) == 1
    payload = ends[0].payload
    assert payload["ok"] is True
    assert payload["content"] == "stdout text"
    assert payload["data"] == {"exit_code": 0, "lines": 2}
    assert payload["error_message"] is None


@pytest.mark.asyncio
async def test_tool_call_end_payload_unknown_tool_carries_error_message() -> None:
    """unknown tool 路径：payload.error_message 非空，content="" data=None。"""
    llm = _StubLLM(
        [
            (None, [ToolCall(call_id="c1", tool_name="missing", arguments={})]),
            ("done", None),
        ]
    )
    sink = _RecordingSink()
    runner = Runner(event_sinks=[sink])
    await runner.run(
        "hi",
        session=InMemorySession("s"),
        agent_spec=AgentSpec(
            name="t",
            instructions="",
            default_model="m",
            tool_names=(),
            max_turns=5,
        ),
        llm=llm,
        tools={},
        approval=_AllowApproval(),
    )

    ends = _find_tool_call_end_events(sink)
    assert len(ends) == 1
    payload = ends[0].payload
    assert payload["ok"] is False
    assert payload["content"] == ""
    assert payload["data"] is None
    assert payload["error_message"] is not None
    assert "not registered" in payload["error_message"]


@pytest.mark.asyncio
async def test_tool_call_end_payload_approval_rejected_carries_error_message() -> None:
    """审批拒绝路径：payload.error_message 含 'approval rejected'。"""
    llm = _StubLLM(
        [
            (None, [ToolCall(call_id="c1", tool_name="rich", arguments={})]),
            ("done", None),
        ]
    )
    sink = _RecordingSink()
    runner = Runner(event_sinks=[sink])
    await runner.run(
        "hi",
        session=InMemorySession("s"),
        agent_spec=AgentSpec(
            name="t",
            instructions="",
            default_model="m",
            tool_names=("rich",),
            max_turns=5,
        ),
        llm=llm,
        tools={"rich": _RichResultTool()},
        approval=_DenyApproval(),
    )

    ends = _find_tool_call_end_events(sink)
    assert len(ends) == 1
    payload = ends[0].payload
    assert payload["ok"] is False
    assert payload["content"] == ""
    assert payload["data"] is None
    assert payload["error_message"] is not None
    assert "approval rejected" in payload["error_message"]
