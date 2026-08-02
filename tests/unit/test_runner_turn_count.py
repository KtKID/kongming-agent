"""unit：覆盖 core.runner 的四种 Result.turn_count 赋值路径。

runner 有四处构造 Result（runner.py:164/182/204/213）：

1. 正常 completed → ``turn_count = state.turn``（当前累计 turn 数）
2. AgentError（含 MaxTurnsExceeded）→ ``turn_count = state.turn``
3. 意外 Exception 兜底 → ``turn_count = state.turn``
4. run.end 事件 payload 里的 ``turn_count`` 必须与 Result.turn_count 一致

本测试只断言 ``turn_count`` 相关行为，不重复 happy-path / max_turns 其它语义
（那些在 ``test_core_runner_basic.py`` 已覆盖）。
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
    PreparedToolCall,
    ToolContext,
    ToolResult,
)
from core.errors import MaxTurnsExceededError, ProviderError
from core.message import Message

# ---------------------------------------------------------------------------
# 最小 stub：本地定义，避免与其它 unit 测共享夹具
# ---------------------------------------------------------------------------


class _StubLLM:
    """按列表顺序 yield 响应；支持在指定 turn 抛异常。"""

    def __init__(
        self,
        responses: list[tuple[str | None, list[ToolCall] | None]] | None = None,
        *,
        raise_on_turn: int | None = None,
        raise_exc: BaseException | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._raise_on_turn = raise_on_turn
        self._raise_exc = raise_exc or RuntimeError("stub boom")
        self._call_count = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self._call_count += 1
        if self._raise_on_turn is not None and self._call_count == self._raise_on_turn:
            raise self._raise_exc
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


class _RecordingSink:
    """收集 emit 到的所有事件，供 run.end 断言。"""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


class _EchoTool:
    """一个最小 tool：返回固定字符串，让 tool-call 闭环能跑。"""

    name = "echo"
    description = "echo"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    async def execute(self, prepared: PreparedToolCall, ctx: ToolContext) -> ToolResult:
        del prepared, ctx
        return ToolResult(ok=True, content="echoed")


# ---------------------------------------------------------------------------
# #2: completed 单 turn → turn_count == 1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_count_single_stop_turn() -> None:
    llm = _StubLLM([("final", None)])
    runner = Runner()
    result = await runner.run(
        "hi",
        session=InMemorySession("s"),
        agent_spec=AgentSpec(name="t", instructions="", default_model="m", max_turns=5),
        llm=llm,
        tools={},
        approval=_AllowApproval(),
    )
    assert result.status == "completed"
    assert result.turn_count == 1


# ---------------------------------------------------------------------------
# #3: tool_call → stop 两 turn 闭环 → turn_count == 2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_count_two_turns_via_tool_call() -> None:
    """第 1 turn 模型发 tool_call，第 2 turn 返回 stop。"""
    llm = _StubLLM(
        [
            (None, [ToolCall(call_id="c1", tool_name="echo", arguments={})]),
            ("done", None),
        ]
    )
    echo = _EchoTool()
    runner = Runner()
    result = await runner.run(
        "hi",
        session=InMemorySession("s"),
        agent_spec=AgentSpec(
            name="t",
            instructions="",
            default_model="m",
            tool_names=("echo",),
            max_turns=5,
        ),
        llm=llm,
        tools={"echo": echo},
        approval=_AllowApproval(),
    )
    assert result.status == "completed"
    assert result.turn_count == 2


@pytest.mark.asyncio
async def test_turn_count_three_turns_via_two_tool_calls() -> None:
    """连续两次 tool_call 后才 stop → turn_count == 3。"""
    llm = _StubLLM(
        [
            (None, [ToolCall(call_id="c1", tool_name="echo", arguments={})]),
            (None, [ToolCall(call_id="c2", tool_name="echo", arguments={})]),
            ("done", None),
        ]
    )
    runner = Runner()
    result = await runner.run(
        "hi",
        session=InMemorySession("s"),
        agent_spec=AgentSpec(
            name="t",
            instructions="",
            default_model="m",
            tool_names=("echo",),
            max_turns=5,
        ),
        llm=llm,
        tools={"echo": _EchoTool()},
        approval=_AllowApproval(),
    )
    assert result.status == "completed"
    assert result.turn_count == 3


# ---------------------------------------------------------------------------
# #4: MaxTurnsExceeded → turn_count == max_turns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("max_turns", [1, 2, 3, 5])
@pytest.mark.asyncio
async def test_turn_count_on_max_turns_exceeded(max_turns: int) -> None:
    """无限发 tool_call 直到撞 max_turns → turn_count == max_turns。"""
    responses = [
        (None, [ToolCall(call_id=f"c{i}", tool_name="echo", arguments={})]) for i in range(10)
    ]
    llm = _StubLLM(responses)
    runner = Runner()
    result = await runner.run(
        "hi",
        session=InMemorySession("s"),
        agent_spec=AgentSpec(
            name="t",
            instructions="",
            default_model="m",
            tool_names=("echo",),
            max_turns=max_turns,
        ),
        llm=llm,
        tools={"echo": _EchoTool()},
        approval=_AllowApproval(),
    )
    assert result.status == "failed"
    assert isinstance(result.error, MaxTurnsExceededError)
    assert result.turn_count == max_turns


# ---------------------------------------------------------------------------
# #5: AgentError 中途抛出 → turn_count == 当前 turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_count_when_provider_error_raised_on_first_turn() -> None:
    """首次 LLM 调用就抛异常 → 被包成 ProviderError，turn_count == 1。"""
    llm = _StubLLM(raise_on_turn=1, raise_exc=RuntimeError("network down"))
    runner = Runner()
    result = await runner.run(
        "hi",
        session=InMemorySession("s"),
        agent_spec=AgentSpec(name="t", instructions="", default_model="m", max_turns=5),
        llm=llm,
        tools={},
        approval=_AllowApproval(),
    )
    assert result.status == "failed"
    assert isinstance(result.error, ProviderError)
    # state.advance_turn() 在调 LLM 之前已执行 → turn=1
    assert result.turn_count == 1


@pytest.mark.asyncio
async def test_turn_count_when_provider_error_raised_on_third_turn() -> None:
    """前两次 tool_call 正常，第三次 LLM 调用抛异常 → turn_count == 3。"""
    llm = _StubLLM(
        [
            (None, [ToolCall(call_id="c1", tool_name="echo", arguments={})]),
            (None, [ToolCall(call_id="c2", tool_name="echo", arguments={})]),
        ],
        raise_on_turn=3,
        raise_exc=RuntimeError("flaky"),
    )
    runner = Runner()
    result = await runner.run(
        "hi",
        session=InMemorySession("s"),
        agent_spec=AgentSpec(
            name="t",
            instructions="",
            default_model="m",
            tool_names=("echo",),
            max_turns=10,
        ),
        llm=llm,
        tools={"echo": _EchoTool()},
        approval=_AllowApproval(),
    )
    assert result.status == "failed"
    assert isinstance(result.error, ProviderError)
    assert result.turn_count == 3


# ---------------------------------------------------------------------------
# #6: _resolve_tools 失败（spec 里声明未注册 tool）→ turn_count == 0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_count_zero_when_tool_resolution_fails() -> None:
    """_resolve_tools 在 _drive_turns 之前触发 → state.advance_turn 尚未执行。"""
    llm = _StubLLM([("never-called", None)])
    runner = Runner()
    result = await runner.run(
        "hi",
        session=InMemorySession("s"),
        agent_spec=AgentSpec(
            name="t",
            instructions="",
            default_model="m",
            tool_names=("ghost",),  # 在 tools lookup 里没有
            max_turns=5,
        ),
        llm=llm,
        tools={},  # 空 ToolLookup
        approval=_AllowApproval(),
    )
    assert result.status == "failed"
    # turn 还没推进 → 0
    assert result.turn_count == 0


# ---------------------------------------------------------------------------
# #7: run.end 事件 payload.turn_count == Result.turn_count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_end_event_turn_count_matches_result() -> None:
    sink = _RecordingSink()
    llm = _StubLLM(
        [
            (None, [ToolCall(call_id="c1", tool_name="echo", arguments={})]),
            ("final", None),
        ]
    )
    runner = Runner(event_sinks=[sink])
    result = await runner.run(
        "hi",
        session=InMemorySession("s"),
        agent_spec=AgentSpec(
            name="t",
            instructions="",
            default_model="m",
            tool_names=("echo",),
            max_turns=5,
        ),
        llm=llm,
        tools={"echo": _EchoTool()},
        approval=_AllowApproval(),
    )

    run_end = [e for e in sink.events if e.kind == "run.end"]
    assert len(run_end) == 1, "必须恰好 emit 一次 run.end"
    payload = run_end[0].payload
    assert payload["turn_count"] == result.turn_count
    assert payload["status"] == result.status
    # 且和实际 turn 递增一致
    assert result.turn_count == 2


@pytest.mark.asyncio
async def test_run_end_event_turn_count_matches_result_on_failure() -> None:
    """失败路径 run.end 的 turn_count 也应与 Result 一致。"""
    sink = _RecordingSink()
    llm = _StubLLM(raise_on_turn=1, raise_exc=RuntimeError("boom"))
    runner = Runner(event_sinks=[sink])
    result = await runner.run(
        "hi",
        session=InMemorySession("s"),
        agent_spec=AgentSpec(name="t", instructions="", default_model="m", max_turns=3),
        llm=llm,
        tools={},
        approval=_AllowApproval(),
    )

    run_end = [e for e in sink.events if e.kind == "run.end"]
    assert len(run_end) == 1
    assert run_end[0].payload["turn_count"] == result.turn_count
    assert run_end[0].payload["status"] == "failed"
    assert result.turn_count == 1
