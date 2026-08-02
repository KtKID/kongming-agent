"""Runner 的显式 LLM 工具调用合同测试。

覆盖流式早停、非流式预校验、瞬态纠错、整响应原子性、资源释放、二次违规
失败和未启用合同时的既有 ``tool_unavailable`` 行为。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from core import AgentSpec, InMemorySession, Runner
from core.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    Event,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    LLMToolCallContract,
    LLMToolCallContractMode,
    PreparedToolCall,
    ToolContext,
    ToolResult,
)
from core.errors import LLMToolCallContractError
from core.message import Message, ToolCall

_STRICT_ONCE = LLMToolCallContract(
    mode=LLMToolCallContractMode.DECLARED_EXACTLY_ONCE,
    correction_retries=1,
)


class _AllowApproval:
    """记录审批请求并固定允许。"""

    def __init__(self) -> None:
        self.requests: list[ApprovalRequest] = []

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(outcome="approved")


class _RecordingSink:
    """记录 Runner 事件。"""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)

    def of_kind(self, kind: str) -> list[Event]:
        return [event for event in self.events if event.kind == kind]


class _RecordingTool:
    """记录真实执行次数的最小 Tool。"""

    name = "evolution_write"
    description = "record one evolution review"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, prepared: PreparedToolCall, ctx: ToolContext) -> ToolResult:
        args = prepared.arguments
        del ctx
        self.calls.append(dict(args))
        return ToolResult(ok=True, content="stored", data={"status": "written"})


class _ClosableChunkIterator:
    """可观察 ``aclose`` 的异步 chunk 迭代器。"""

    def __init__(
        self,
        chunks: list[LLMStreamChunk],
        *,
        close_error: Exception | None = None,
    ) -> None:
        self._chunks = list(chunks)
        self._index = 0
        self._close_error = close_error
        self.closed = False

    def __aiter__(self) -> _ClosableChunkIterator:
        return self

    async def __anext__(self) -> LLMStreamChunk:
        if self.closed or self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk

    async def aclose(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


class _ScriptedStreamProvider:
    """每次请求返回一组独立、可关闭的 chunk 迭代器。"""

    def __init__(
        self,
        scripts: list[list[LLMStreamChunk]],
        *,
        close_errors: list[Exception | None] | None = None,
    ) -> None:
        self._scripts = [list(script) for script in scripts]
        self._close_errors = list(close_errors or ())
        self.requests: list[LLMRequest] = []
        self.iterators: list[_ClosableChunkIterator] = []

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        self.requests.append(request)
        chunks = self._scripts.pop(0)
        close_error = self._close_errors.pop(0) if self._close_errors else None
        iterator = _ClosableChunkIterator(chunks, close_error=close_error)
        self.iterators.append(iterator)
        return iterator

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise AssertionError(f"stream path expected, got complete request {request.model}")


class _NonClosableChunkIterator:
    """缺少 ``aclose`` 的合法 AsyncIterator，用于验证严格合同 fail-fast。"""

    def __init__(self, chunks: list[LLMStreamChunk]) -> None:
        self._chunks = list(chunks)
        self._index = 0

    def __aiter__(self) -> _NonClosableChunkIterator:
        return self

    async def __anext__(self) -> LLMStreamChunk:
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


class _NonClosableStreamProvider:
    """返回不可关闭 iterator 的流式 provider。"""

    def __init__(self, chunks: list[LLMStreamChunk]) -> None:
        self.iterator = _NonClosableChunkIterator(chunks)
        self.requests: list[LLMRequest] = []

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        self.requests.append(request)
        return self.iterator

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise AssertionError(f"stream path expected, got complete request {request.model}")


class _ScriptedCompleteProvider:
    """非流式脚本 provider。"""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return self._responses.pop(0)


def _tool_call(call_id: str, tool_name: str) -> ToolCall:
    return ToolCall(call_id=call_id, tool_name=tool_name, arguments={"value": call_id})


def _tool_response(*calls: ToolCall, content: str | None = None) -> LLMResponse:
    return LLMResponse(
        message=Message.assistant(content, tool_calls=list(calls)),
        finish_reason="tool_calls" if calls else "stop",
    )


def _stream_chunks(*calls: ToolCall, content: str | None = None) -> list[LLMStreamChunk]:
    chunks = [
        LLMStreamChunk(
            kind="tool_call.start",
            index=index,
            tool_call_id=call.call_id,
            tool_name=call.tool_name,
        )
        for index, call in enumerate(calls)
    ]
    chunks.append(
        LLMStreamChunk(
            kind="message.done",
            message=Message.assistant(content, tool_calls=list(calls)),
            finish_reason="tool_calls" if calls else "stop",
        )
    )
    return chunks


def _runner_inputs() -> tuple[AgentSpec, InMemorySession, _RecordingTool, _AllowApproval]:
    spec = AgentSpec(
        name="evolution-reviewer",
        instructions="review",
        default_model="test-model",
        tool_names=("evolution_write",),
        max_turns=3,
    )
    return spec, InMemorySession("evo-review-thread-1-run-1"), _RecordingTool(), _AllowApproval()


@pytest.mark.unit
async def test_stream_contract_retries_once_with_transient_correction_and_clean_history() -> None:
    invalid = _tool_call("bad-1", "documents")
    valid = _tool_call("write-1", "evolution_write")
    provider = _ScriptedStreamProvider(
        [
            [
                LLMStreamChunk(kind="content.delta", delta="rejected-stream-content"),
                *_stream_chunks(invalid),
                LLMStreamChunk(kind="content.delta", delta="must-not-be-consumed"),
            ],
            _stream_chunks(valid),
        ]
    )
    sink = _RecordingSink()
    spec, session, tool, approval = _runner_inputs()

    result = await Runner(stream_enabled=True, event_sinks=[sink]).run(
        "review this transcript",
        session=session,
        agent_spec=spec,
        llm=provider,
        tools={tool.name: tool},
        approval=approval,
        max_turns=1,
        llm_tool_call_contract=_STRICT_ONCE,
    )

    assert result.status == "completed"
    assert len(provider.requests) == 2
    assert provider.iterators[0].closed is True
    assert tool.calls == [{"value": "write-1"}]
    assert len(approval.requests) == 1
    correction = provider.requests[1].messages[-1]
    assert correction.role == "user"
    assert "documents" in (correction.content or "")
    assert "evolution_write" in (correction.content or "")
    history = await session.history()
    assert [call.tool_name for msg in history for call in (msg.tool_calls or ())] == [
        "evolution_write"
    ]
    assert all("documents" not in (message.content or "") for message in history)
    assert len(sink.of_kind("llm.tool_call.contract_violation")) == 1
    assert all(
        event.payload.get("delta") != "rejected-stream-content"
        for event in sink.of_kind("content.delta")
    )
    assert not sink.of_kind("run.cancelled")


@pytest.mark.unit
async def test_stream_contract_second_violation_fails_without_cancel_or_tool_events() -> None:
    provider = _ScriptedStreamProvider(
        [
            _stream_chunks(_tool_call("bad-1", "documents")),
            _stream_chunks(_tool_call("bad-2", "documents")),
        ]
    )
    sink = _RecordingSink()
    spec, session, tool, approval = _runner_inputs()

    result = await Runner(stream_enabled=True, event_sinks=[sink]).run(
        "review",
        session=session,
        agent_spec=spec,
        llm=provider,
        tools={tool.name: tool},
        approval=approval,
        llm_tool_call_contract=_STRICT_ONCE,
    )

    assert result.status == "failed"
    assert isinstance(result.error, LLMToolCallContractError)
    assert len(provider.requests) == 2
    assert len(sink.of_kind("llm.tool_call.contract_violation")) == 2
    assert not sink.of_kind("tool.call.start")
    assert not sink.of_kind("tool.call.end")
    assert not sink.of_kind("run.cancelled")
    assert tool.calls == []
    history = await session.history()
    assert all(message.role != "assistant" for message in history)


@pytest.mark.unit
async def test_stream_close_error_preserves_violation_log_and_corrective_retry() -> None:
    invalid = _tool_call("bad-1", "documents")
    valid = _tool_call("write-1", "evolution_write")
    provider = _ScriptedStreamProvider(
        [_stream_chunks(invalid), _stream_chunks(valid)],
        close_errors=[RuntimeError("close failed"), None],
    )
    sink = _RecordingSink()
    spec, session, tool, approval = _runner_inputs()

    result = await Runner(stream_enabled=True, event_sinks=[sink]).run(
        "review",
        session=session,
        agent_spec=spec,
        llm=provider,
        tools={tool.name: tool},
        approval=approval,
        max_turns=1,
        llm_tool_call_contract=_STRICT_ONCE,
    )

    assert result.status == "completed"
    assert len(provider.requests) == 2
    assert provider.iterators[0].closed is True
    assert len(sink.of_kind("llm.tool_call.contract_violation")) == 1
    assert tool.calls == [{"value": "write-1"}]


@pytest.mark.unit
async def test_strict_stream_rejects_non_closable_iterator_before_consumption() -> None:
    provider = _NonClosableStreamProvider(_stream_chunks(_tool_call("bad-1", "documents")))
    sink = _RecordingSink()
    spec, session, tool, approval = _runner_inputs()

    result = await Runner(stream_enabled=True, event_sinks=[sink]).run(
        "review",
        session=session,
        agent_spec=spec,
        llm=provider,
        tools={tool.name: tool},
        approval=approval,
        llm_tool_call_contract=_STRICT_ONCE,
    )

    assert result.status == "failed"
    assert result.error is not None
    assert type(result.error).__name__ == "ProviderError"
    assert provider.iterator._index == 0
    assert sink.of_kind("tool.call.start") == []
    assert sink.of_kind("run.cancelled") == []


@pytest.mark.unit
async def test_non_stream_contract_rejects_before_content_session_and_tool_side_effects() -> None:
    invalid = _tool_call("bad-1", "documents")
    valid = _tool_call("write-1", "evolution_write")
    provider = _ScriptedCompleteProvider(
        [
            _tool_response(invalid, content="rejected-content"),
            _tool_response(valid),
        ]
    )
    sink = _RecordingSink()
    spec, session, tool, approval = _runner_inputs()

    result = await Runner(stream_enabled=False, event_sinks=[sink]).run(
        "review",
        session=session,
        agent_spec=spec,
        llm=provider,
        tools={tool.name: tool},
        approval=approval,
        max_turns=1,
        llm_tool_call_contract=_STRICT_ONCE,
    )

    assert result.status == "completed"
    assert len(provider.requests) == 2
    assert tool.calls == [{"value": "write-1"}]
    assert all(
        event.payload.get("delta") != "rejected-content" for event in sink.of_kind("content.delta")
    )
    assert len(sink.of_kind("llm.response")) == 1
    assert sink.of_kind("usage") == []
    assert len(sink.of_kind("tool.call.start")) == 1
    assert len(sink.of_kind("tool.call.end")) == 1
    assert len(approval.requests) == 1
    history = await session.history()
    assert all(message.content != "rejected-content" for message in history)


@pytest.mark.unit
async def test_contract_missing_required_call_retries_with_distinct_violation() -> None:
    valid = _tool_call("write-1", "evolution_write")
    provider = _ScriptedCompleteProvider(
        [
            _tool_response(content="forgot tool"),
            _tool_response(valid),
        ]
    )
    sink = _RecordingSink()
    spec, session, tool, approval = _runner_inputs()

    result = await Runner(event_sinks=[sink]).run(
        "review",
        session=session,
        agent_spec=spec,
        llm=provider,
        tools={tool.name: tool},
        approval=approval,
        max_turns=1,
        llm_tool_call_contract=_STRICT_ONCE,
    )

    assert result.status == "completed"
    violation = sink.of_kind("llm.tool_call.contract_violation")[0]
    assert violation.payload["violation_kind"] == "missing_required_tool_call"
    assert tool.calls == [{"value": "write-1"}]


@pytest.mark.unit
async def test_contract_rejects_second_declared_call_before_any_execution() -> None:
    first = _tool_call("write-1", "evolution_write")
    second = _tool_call("write-2", "evolution_write")
    provider = _ScriptedStreamProvider([_stream_chunks(first, second)])
    sink = _RecordingSink()
    spec, session, tool, approval = _runner_inputs()
    contract = LLMToolCallContract(
        mode=LLMToolCallContractMode.DECLARED_EXACTLY_ONCE,
        correction_retries=0,
    )

    result = await Runner(stream_enabled=True, event_sinks=[sink]).run(
        "review",
        session=session,
        agent_spec=spec,
        llm=provider,
        tools={tool.name: tool},
        approval=approval,
        llm_tool_call_contract=contract,
    )

    assert result.status == "failed"
    assert tool.calls == []
    violation = sink.of_kind("llm.tool_call.contract_violation")[0]
    assert violation.payload["violation_kind"] == "tool_call_limit_exceeded"
    assert violation.payload["tool_call_id"] == "write-2"


@pytest.mark.unit
async def test_contract_discards_valid_prefix_when_later_tool_is_undeclared() -> None:
    valid_prefix = _tool_call("write-1", "evolution_write")
    invalid_tail = _tool_call("bad-1", "documents")
    provider = _ScriptedStreamProvider([_stream_chunks(valid_prefix, invalid_tail)])
    sink = _RecordingSink()
    spec, session, tool, approval = _runner_inputs()
    contract = LLMToolCallContract(
        mode=LLMToolCallContractMode.DECLARED_EXACTLY_ONCE,
        correction_retries=0,
    )

    result = await Runner(stream_enabled=True, event_sinks=[sink]).run(
        "review",
        session=session,
        agent_spec=spec,
        llm=provider,
        tools={tool.name: tool},
        approval=approval,
        llm_tool_call_contract=contract,
    )

    assert result.status == "failed"
    assert tool.calls == []
    assert approval.requests == []
    history = await session.history()
    assert all(message.role != "assistant" for message in history)
    violation = sink.of_kind("llm.tool_call.contract_violation")[0]
    assert violation.payload["violation_kind"] == "undeclared_tool"
    assert violation.payload["tool_name"] == "documents"


@pytest.mark.unit
async def test_non_stream_contract_discards_valid_prefix_before_accepted_events() -> None:
    valid_prefix = _tool_call("write-1", "evolution_write")
    invalid_tail = _tool_call("bad-1", "documents")
    provider = _ScriptedCompleteProvider([_tool_response(valid_prefix, invalid_tail)])
    sink = _RecordingSink()
    spec, session, tool, approval = _runner_inputs()
    contract = LLMToolCallContract(
        mode=LLMToolCallContractMode.DECLARED_EXACTLY_ONCE,
        correction_retries=0,
    )

    result = await Runner(event_sinks=[sink]).run(
        "review",
        session=session,
        agent_spec=spec,
        llm=provider,
        tools={tool.name: tool},
        approval=approval,
        llm_tool_call_contract=contract,
    )

    assert result.status == "failed"
    assert isinstance(result.error, LLMToolCallContractError)
    assert tool.calls == []
    assert approval.requests == []
    assert sink.of_kind("content.delta") == []
    assert sink.of_kind("llm.response") == []
    assert sink.of_kind("usage") == []
    assert sink.of_kind("tool.call.start") == []
    assert sink.of_kind("tool.call.end") == []
    history = await session.history()
    assert all(message.role != "assistant" for message in history)
    violation = sink.of_kind("llm.tool_call.contract_violation")[0]
    assert violation.payload["violation_kind"] == "undeclared_tool"
    assert violation.payload["tool_name"] == "documents"


@pytest.mark.unit
async def test_runner_without_contract_keeps_tool_unavailable_behavior() -> None:
    unknown = _tool_call("bad-1", "documents")
    provider = _ScriptedCompleteProvider(
        [
            _tool_response(unknown),
            _tool_response(content="done"),
        ]
    )
    sink = _RecordingSink()
    spec, session, tool, approval = _runner_inputs()

    result = await Runner(event_sinks=[sink]).run(
        "review",
        session=session,
        agent_spec=spec,
        llm=provider,
        tools={tool.name: tool},
        approval=approval,
        max_turns=2,
    )

    assert result.status == "completed"
    history = await session.history()
    unavailable = next(message for message in history if message.role == "tool")
    assert unavailable.metadata["reason"] == "tool_unavailable"
    assert not sink.of_kind("llm.tool_call.contract_violation")
