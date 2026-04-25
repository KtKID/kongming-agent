"""E#4：流式 fallback 路径与开关行为。

覆盖：
- stream_enabled=False 时 runner 走非流式路径，不 emit 流式事件
- isinstance(SupportsLLMStream) 探测失败（provider 不实现 stream）→ 回退非流式
- suppress_content_after_tool_call=False 时 tool_call 后 content 仍 emit
- 流式 disabled 时 stub.complete 被调用而非 stub.stream
"""

from __future__ import annotations

import pytest

from core import AgentSpec, InMemorySession, Runner
from core.contracts import LLMStreamChunk
from core.message import Message, ToolCall
from tests.e2e.conftest import (
    MemoryEventSink,
    RecordingApproval,
    StubLLMProvider,
    StubLLMStreamProvider,
)


def _spec() -> AgentSpec:
    return AgentSpec(name="t", instructions="x", default_model="m1", max_turns=3)


@pytest.mark.e2e
async def test_e4_1_stream_disabled_uses_complete_not_stream() -> None:
    """E.4.1：stream_enabled=False 时 runner 走 complete()，不 emit 流式事件。"""
    chunks = [
        LLMStreamChunk(kind="content.delta", delta="ab", index=0),
        LLMStreamChunk(
            kind="message.done", message=Message.assistant(content="ab"), finish_reason="stop"
        ),
    ]
    sink = MemoryEventSink()
    runner = Runner(stream_enabled=False, event_sinks=[sink])
    stub = StubLLMStreamProvider()
    stub.script_chunks(chunks)

    res = await runner.run(
        "hi", session=InMemorySession("fb1"), agent_spec=_spec(),
        llm=stub, tools={}, approval=RecordingApproval(),
    )

    assert res.status == "completed"
    # 关键断言：流式事件全 0
    assert len(sink.of_kind("content.delta")) == 0
    assert len(sink.of_kind("reasoning.delta")) == 0
    assert len(sink.of_kind("llm.chunk.first")) == 0
    assert len(sink.of_kind("llm.stream.end")) == 0


@pytest.mark.e2e
async def test_e4_2_isinstance_probe_fallback_when_provider_lacks_stream() -> None:
    """E.4.2：provider 不实现 stream() 时 isinstance 探测为 False，走非流式。

    StubLLMProvider 只实现 complete()，不实现 stream() → isinstance(SupportsLLMStream)=False
    → runner 即使 stream_enabled=True 也回退到 _safe_llm_complete。
    """
    sink = MemoryEventSink()
    runner = Runner(stream_enabled=True, event_sinks=[sink])
    stub = StubLLMProvider()
    stub.script(content="hi via complete")

    res = await runner.run(
        "ping", session=InMemorySession("fb2"), agent_spec=_spec(),
        llm=stub, tools={}, approval=RecordingApproval(),
    )

    assert res.status == "completed"
    assert res.final_message.content == "hi via complete"
    # 没有任何流式事件
    assert len(sink.of_kind("llm.chunk.first")) == 0
    assert len(sink.of_kind("llm.stream.end")) == 0


@pytest.mark.e2e
async def test_e4_3_suppress_off_emits_content_after_tool_call() -> None:
    """E.4.3：suppress=False 时 tool_call 后的 content.delta 也会 emit。"""
    tc = ToolCall(call_id="c1", tool_name="t", arguments={})
    turn1 = [
        LLMStreamChunk(kind="content.delta", delta="pre", index=0),
        LLMStreamChunk(kind="tool_call.start", index=0, tool_call_id="c1", tool_name="t"),
        LLMStreamChunk(kind="content.delta", delta="post-no-suppress", index=0),
        LLMStreamChunk(kind="tool_call.end", index=0, tool_call_id="c1", tool_name="t"),
        LLMStreamChunk(
            kind="message.done",
            message=Message.assistant(content="pre", tool_calls=[tc]),
            finish_reason="tool_calls",
        ),
    ]

    sink = MemoryEventSink()
    runner = Runner(
        stream_enabled=True, suppress_content_after_tool_call=False, event_sinks=[sink],
    )
    stub = StubLLMStreamProvider()
    stub.script_chunks(turn1)

    spec = AgentSpec(name="t", instructions="x", default_model="m1", max_turns=1)
    await runner.run(
        "x", session=InMemorySession("fb3"), agent_spec=spec,
        llm=stub, tools={}, approval=RecordingApproval(),
    )

    deltas = [e.payload["delta"] for e in sink.of_kind("content.delta")]
    # suppress=False：post-no-suppress 也 emit
    assert "pre" in deltas
    assert "post-no-suppress" in deltas


@pytest.mark.e2e
async def test_e4_4_stream_enabled_with_streaming_provider_emits_events() -> None:
    """E.4.4：stream_enabled=True + provider 实现 stream → 走流式路径，发流式事件。"""
    chunks = [
        LLMStreamChunk(kind="content.delta", delta="hello", index=0),
        LLMStreamChunk(
            kind="message.done", message=Message.assistant(content="hello"), finish_reason="stop"
        ),
    ]
    sink = MemoryEventSink()
    runner = Runner(stream_enabled=True, event_sinks=[sink])
    stub = StubLLMStreamProvider()
    stub.script_chunks(chunks)

    res = await runner.run(
        "hi", session=InMemorySession("fb4"), agent_spec=_spec(),
        llm=stub, tools={}, approval=RecordingApproval(),
    )

    assert res.status == "completed"
    # 流式事件齐全
    assert len(sink.of_kind("content.delta")) == 1
    assert len(sink.of_kind("llm.chunk.first")) == 1
    assert len(sink.of_kind("llm.stream.end")) == 1
