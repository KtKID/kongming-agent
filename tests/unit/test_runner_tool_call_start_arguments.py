"""unit：tool.call.start 事件补发 arguments（track-timeline-v0.1 · task-tool-call-arguments）。

验证 feat07 场景1 的 wire 全链路：

1. Runner 执行含参 tool call 时，``tool.call.start`` 事件 payload 携带真实
   ``arguments``（此前只发 call_id / tool_name，下游永远拿到空参数）。
2. 该事件经真实 :class:`hosts.web.websocket.event_sink.WSEventSink` 翻译后，
   ``tool.call.start`` 帧的 ``arguments`` 字段保留真实参数——聊天工具卡片
   （MessageList 已渲染 arguments）与轨迹详情入参展示共同依赖此字段。

关键函数：

- ``_run_with_echo_tool``：装配 stub LLM + echo 工具跑一次完整 run，
  返回事件捕获 sink，供两个用例复用同一事件流。
- ``test_tool_call_start_payload_contains_arguments``：断言事件 payload 层
  arguments 完整（场景1 断言点 1）。
- ``test_tool_call_start_event_translates_to_frame_with_arguments``：断言
  WS 帧层 arguments 不丢失（场景1 断言点 2，wire 链路）。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from core import AgentSpec, InMemorySession, Runner, ToolCall
from core.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    Event,
    LLMRequest,
    LLMResponse,
    PreparedToolCall,
    ToolContext,
    ToolResult,
)
from core.message import Message
from hosts.web.websocket.event_sink import WSEventSink


class _RecordingSink:
    """记录收到的 Event，供 tool.call.start payload 断言。"""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


class _StubLLM:
    """两段式 stub：第 1 轮返回含参 tool_call，第 2 轮返回纯文本收尾。"""

    def __init__(self) -> None:
        self._responses: list[LLMResponse] = [
            LLMResponse(
                message=Message(
                    role="assistant",
                    content="",
                    tool_calls=(
                        ToolCall(
                            call_id="c1",
                            tool_name="echo_args",
                            arguments={"path": "src/core/runner.py"},
                        ),
                    ),
                ),
                finish_reason="tool_calls",
            ),
            LLMResponse(
                message=Message(role="assistant", content="done"),
                finish_reason="stop",
            ),
        ]

    async def complete(self, request: LLMRequest) -> LLMResponse:
        del request
        return self._responses.pop(0)


class _EchoTool:
    """记录入参并返回成功的哑工具。"""

    name = "echo_args"
    description = "echo arguments for trajectory test"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.arguments: list[dict[str, Any]] = []

    async def execute(
        self,
        prepared: PreparedToolCall,
        ctx: ToolContext,
    ) -> ToolResult:
        del ctx
        self.arguments.append(dict(prepared.arguments))
        return ToolResult(ok=True, content="ok")


class _AllowApproval:
    """自动放行的审批器。"""

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        del request
        return ApprovalDecision(outcome="approved")


async def _run_with_echo_tool() -> tuple[_RecordingSink, list[Event]]:
    """跑一次含参 tool call 的完整 run，返回事件 sink 与 tool.call.start 事件。"""
    sink = _RecordingSink()
    runner = Runner()
    session = InMemorySession("u")
    spec = AgentSpec(name="t", instructions="s", default_model="m", max_turns=3)

    result = await runner.run(
        "hi",
        session=session,
        agent_spec=spec,
        llm=_StubLLM(),
        tools={"echo_args": _EchoTool()},
        approval=_AllowApproval(),
        event_sinks=[sink],
    )
    assert result.status == "completed"
    starts = [event for event in sink.events if event.kind == "tool.call.start"]
    assert len(starts) == 1
    return sink, starts


@pytest.mark.unit
async def test_tool_call_start_payload_contains_arguments() -> None:
    """事件 payload 层：tool.call.start 携带真实 arguments。"""
    _, starts = await _run_with_echo_tool()

    assert starts[0].payload["call_id"] == "c1"
    assert starts[0].payload["tool_name"] == "echo_args"
    assert starts[0].payload["arguments"] == {"path": "src/core/runner.py"}


@pytest.mark.unit
async def test_tool_call_start_event_translates_to_frame_with_arguments() -> None:
    """WS 帧层：经真实 WSEventSink 翻译，帧内 arguments 保留真实参数。"""
    _, starts = await _run_with_echo_tool()

    ws = AsyncMock()
    ws.send_json = AsyncMock(return_value=None)
    ws_sink = WSEventSink(ws, thread_id="t1")
    await ws_sink.emit(starts[0])

    ws.send_json.assert_awaited_once()
    frame = ws.send_json.await_args[0][0]
    assert frame["frame_type"] == "tool.call.start"
    assert frame["call_id"] == "c1"
    assert frame["arguments"] == {"path": "src/core/runner.py"}
