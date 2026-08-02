"""unit：Runner lifecycle hook 边界。"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest

from core import LIFECYCLE_HOOK_POINTS, AgentSpec, InMemorySession, LifecycleHook, Runner
from core.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    Event,
    LLMRequest,
    LLMResponse,
    PreparedToolCall,
    Session,
    ToolContext,
    ToolResult,
)
from core.message import Message, ToolCall
from core.result import Result
from core.run_state import RunState


class _StubLLM:
    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            message=Message.assistant("done"),
            finish_reason="stop",
        )


class _FailingLLM:
    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise RuntimeError("llm boom")


class _CancelledLLM:
    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise asyncio.CancelledError


class _AllowApproval:
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(outcome="approved")


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


class _ScriptedToolLLM:
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            return LLMResponse(
                message=Message.assistant(
                    tool_calls=[
                        ToolCall(
                            call_id="call-echo",
                            tool_name="echo_tool",
                            arguments={"text": "hello"},
                        )
                    ]
                ),
                finish_reason="tool_calls",
            )
        return LLMResponse(
            message=Message.assistant("final answer"),
            finish_reason="stop",
        )


class _EchoTool:
    name = "echo_tool"
    description = "echo test tool"
    input_schema: dict[str, Any] = {"type": "object"}

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], ToolContext]] = []

    async def execute(self, prepared: PreparedToolCall, ctx: ToolContext) -> ToolResult:
        args = prepared.arguments
        self.calls.append((args, ctx))
        return ToolResult(ok=True, content=f"echo:{args['text']}", data={"seen": args["text"]})


class _LifecycleBoundaryRecorder:
    def __init__(self, *, sink: _RecordingSink, session: Session) -> None:
        self._sink = sink
        self._session = session
        self.records: list[dict[str, Any]] = []

    async def _record(self, phase: str, state: RunState, **extra: Any) -> None:
        history = await self._session.history()
        self.records.append(
            {
                "phase": phase,
                "turn": state.turn,
                "events": [event.kind for event in self._sink.events],
                "history": [
                    {
                        "role": message.role,
                        "content": message.content,
                        "name": message.name,
                        "tool_call_id": message.tool_call_id,
                        "tool_calls": [call.tool_name for call in message.tool_calls or ()],
                    }
                    for message in history
                ],
                **extra,
            }
        )

    async def before_turn(self, state: RunState) -> None:
        await self._record("before_turn", state)

    async def after_turn(self, state: RunState, assistant_message: Message) -> None:
        await self._record(
            "after_turn",
            state,
            assistant_content=assistant_message.content,
            assistant_tool_calls=[call.tool_name for call in assistant_message.tool_calls or ()],
        )

    async def before_tool(self, state: RunState, call: ToolCall) -> None:
        await self._record("before_tool", state, tool_name=call.tool_name)

    async def after_tool(self, state: RunState, call: ToolCall, result_message: Message) -> None:
        await self._record(
            "after_tool",
            state,
            tool_name=call.tool_name,
            result_role=result_message.role,
            result_name=result_message.name,
            result_content=result_message.content,
        )

    async def after_run(self, state: RunState, session: Session, result: Result) -> None:
        await self._record("after_run", state, result_status=result.status)


class _AfterRunRecorder:
    def __init__(self, sink: _RecordingSink) -> None:
        self._sink = sink
        self.calls: list[tuple[str, str, str]] = []
        self.event_kinds_seen_at_hook: list[str] = []

    async def before_turn(self, state: RunState) -> None:
        return None

    async def after_turn(self, state: RunState, assistant_message: Message) -> None:
        return None

    async def before_tool(self, state: RunState, call: ToolCall) -> None:
        return None

    async def after_tool(self, state: RunState, call: ToolCall, result_message: Message) -> None:
        return None

    async def after_run(self, state: RunState, session: Session, result: Result) -> None:
        self.calls.append((session.session_id, state.run_id, result.status))
        self.event_kinds_seen_at_hook.extend(event.kind for event in self._sink.events)


class _BadLifecycleHook:
    async def before_turn(self, state: RunState) -> None:
        return None

    async def after_turn(self, state: RunState, assistant_message: Message) -> None:
        return None

    async def before_tool(self, state: RunState, call: ToolCall) -> None:
        return None

    async def after_tool(self, state: RunState, call: ToolCall, result_message: Message) -> None:
        return None

    async def after_run(self, state: RunState, session: Session, result: Result) -> None:
        raise RuntimeError("hook boom")


def _agent_spec() -> AgentSpec:
    return AgentSpec(name="t", instructions="", default_model="m", max_turns=3)


def _event_count(record: dict[str, Any], kind: str) -> int:
    return record["events"].count(kind)


@pytest.mark.unit
async def test_lifecycle_hooks_follow_real_runner_order_and_boundaries() -> None:
    sink = _RecordingSink()
    session = InMemorySession("hook-flow")
    hook = _LifecycleBoundaryRecorder(sink=sink, session=session)
    llm = _ScriptedToolLLM()
    tool = _EchoTool()

    result = await Runner(event_sinks=[sink]).run(
        "hi",
        session=session,
        agent_spec=AgentSpec(
            name="t",
            instructions="",
            default_model="m",
            tool_names=("echo_tool",),
            max_turns=4,
        ),
        llm=llm,
        tools={"echo_tool": tool},
        approval=_AllowApproval(),
        lifecycle_hooks=[hook],
    )

    assert result.status == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "final answer"
    assert [record["phase"] for record in hook.records] == [
        "before_turn",
        "after_turn",
        "before_tool",
        "after_tool",
        "before_turn",
        "after_turn",
        "after_run",
    ]

    first_before_turn = hook.records[0]
    assert first_before_turn["turn"] == 1
    assert "run.start" in first_before_turn["events"]
    assert "turn.start" in first_before_turn["events"]
    assert "llm.request" not in first_before_turn["events"]
    assert [message["role"] for message in first_before_turn["history"]] == ["user"]

    first_after_turn = hook.records[1]
    assert "llm.request" in first_after_turn["events"]
    assert "llm.response" in first_after_turn["events"]
    assert "turn.end" not in first_after_turn["events"]
    assert first_after_turn["assistant_tool_calls"] == ["echo_tool"]
    assert [message["role"] for message in first_after_turn["history"]] == [
        "user",
        "assistant",
    ]
    assert first_after_turn["history"][-1]["tool_calls"] == ["echo_tool"]

    before_tool = hook.records[2]
    assert before_tool["tool_name"] == "echo_tool"
    assert "turn.end" in before_tool["events"]
    assert "tool.call.start" not in before_tool["events"]
    assert "approval.request" not in before_tool["events"]

    after_tool = hook.records[3]
    assert after_tool["result_role"] == "tool"
    assert after_tool["result_name"] == "echo_tool"
    assert after_tool["result_content"] == "echo:hello"
    assert "tool.call.start" in after_tool["events"]
    assert "approval.request" in after_tool["events"]
    assert "approval.decision" in after_tool["events"]
    assert "tool.call.end" in after_tool["events"]
    assert [message["role"] for message in after_tool["history"]] == [
        "user",
        "assistant",
        "tool",
    ]

    second_before_turn = hook.records[4]
    assert second_before_turn["turn"] == 2
    assert _event_count(second_before_turn, "llm.request") == 1
    assert _event_count(second_before_turn, "turn.start") == 2

    second_after_turn = hook.records[5]
    assert second_after_turn["assistant_content"] == "final answer"
    assert second_after_turn["assistant_tool_calls"] == []
    assert "run.end" not in second_after_turn["events"]
    assert [message["role"] for message in second_after_turn["history"]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]

    after_run = hook.records[6]
    assert after_run["result_status"] == "completed"
    assert "run.end" not in after_run["events"]
    assert sink.events[-1].kind == "run.end"
    assert len(llm.requests) == 2
    assert len(tool.calls) == 1


@pytest.mark.unit
def test_lifecycle_hook_point_specs_match_runner_dispatchers() -> None:
    runner_source = inspect.getsource(Runner)
    names = [point.name for point in LIFECYCLE_HOOK_POINTS]

    assert names == [
        "before_turn",
        "after_turn",
        "before_tool",
        "after_tool",
        "after_run",
    ]
    for point in LIFECYCLE_HOOK_POINTS:
        assert hasattr(LifecycleHook, point.method_name)
        dispatcher_name = f"_run_lifecycle_{point.name}"
        assert hasattr(Runner, dispatcher_name)
        assert runner_source.count(dispatcher_name) >= 2
        assert point.timing.strip()
        assert point.payload.strip()
        assert point.examples.strip()


@pytest.mark.unit
async def test_lifecycle_after_run_runs_inside_runner_before_run_end() -> None:
    sink = _RecordingSink()
    hook = _AfterRunRecorder(sink)

    result = await Runner(event_sinks=[sink]).run(
        "hi",
        session=InMemorySession("s"),
        agent_spec=_agent_spec(),
        llm=_StubLLM(),
        tools={},
        approval=_AllowApproval(),
        lifecycle_hooks=[hook],
    )

    assert result.status == "completed"
    assert hook.calls == [("s", result.run_id, "completed")]
    assert "run.end" not in hook.event_kinds_seen_at_hook
    assert sink.events[-1].kind == "run.end"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("llm", "expected_status"),
    [
        (_FailingLLM(), "failed"),
        (_CancelledLLM(), "cancelled"),
    ],
)
async def test_lifecycle_after_run_runs_for_terminal_results(
    llm: object,
    expected_status: str,
) -> None:
    sink = _RecordingSink()
    hook = _AfterRunRecorder(sink)

    result = await Runner(event_sinks=[sink]).run(
        "hi",
        session=InMemorySession("s"),
        agent_spec=_agent_spec(),
        llm=llm,  # type: ignore[arg-type]
        tools={},
        approval=_AllowApproval(),
        lifecycle_hooks=[hook],
    )

    assert result.status == expected_status
    assert hook.calls == [("s", result.run_id, expected_status)]
    assert "run.end" not in hook.event_kinds_seen_at_hook
    assert sink.events[-1].kind == "run.end"


@pytest.mark.unit
async def test_lifecycle_after_run_error_is_reported_without_changing_result() -> None:
    sink = _RecordingSink()

    result = await Runner(event_sinks=[sink]).run(
        "hi",
        session=InMemorySession("s"),
        agent_spec=_agent_spec(),
        llm=_StubLLM(),
        tools={},
        approval=_AllowApproval(),
        lifecycle_hooks=[_BadLifecycleHook()],
    )

    assert result.status == "completed"
    errors = [
        event
        for event in sink.events
        if event.kind == "error" and event.payload.get("source") == "lifecycle_hook"
    ]
    assert len(errors) == 1
    assert errors[0].payload["phase"] == "after_run"
    assert errors[0].payload["type"] == "RuntimeError"
    assert errors[0].payload["message"] == "hook boom"
    assert sink.events[-1].kind == "run.end"
