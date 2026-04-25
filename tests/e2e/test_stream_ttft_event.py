"""E#2：TTFT 事件断言。

- 一次 stream run 必须 emit 恰好一次 `llm.chunk.first`
- payload 含 `elapsed_ms: int` + `model: str | None`
"""

from __future__ import annotations

import pytest

from core import AgentSpec, InMemorySession, Runner
from core.contracts import LLMStreamChunk
from core.message import Message
from tests.e2e.conftest import MemoryEventSink, RecordingApproval, StubLLMStreamProvider


def _spec() -> AgentSpec:
    return AgentSpec(
        name="t", instructions="x", default_model="m1", max_turns=3,
    )


@pytest.mark.e2e
async def test_e2_1_ttft_event_emitted_once_per_run() -> None:
    """E.2.1：一次 stream run 必须 emit 恰好一次 llm.chunk.first。"""
    chunks = [
        LLMStreamChunk(kind="content.delta", delta="a", index=0),
        LLMStreamChunk(kind="content.delta", delta="b", index=0),
        LLMStreamChunk(
            kind="message.done", message=Message.assistant(content="ab"), finish_reason="stop"
        ),
    ]
    sink = MemoryEventSink()
    runner = Runner(stream_enabled=True, event_sinks=[sink])
    stub = StubLLMStreamProvider()
    stub.script_chunks(chunks)

    await runner.run(
        "hi", session=InMemorySession("ttft1"), agent_spec=_spec(),
        llm=stub, tools={}, approval=RecordingApproval(),
    )

    ttft_events = sink.of_kind("llm.chunk.first")
    assert len(ttft_events) == 1, f"应该恰好 1 次 ttft，实际 {len(ttft_events)}"
    payload = ttft_events[0].payload
    assert "elapsed_ms" in payload and isinstance(payload["elapsed_ms"], int)
    assert payload["elapsed_ms"] >= 0
    assert payload.get("model") == "m1"


@pytest.mark.e2e
async def test_e2_2_ttft_not_emitted_when_only_message_done() -> None:
    """E.2.2：流只有 message.done（无 content / reasoning delta）时仍 emit ttft 一次。

    按 plan，TTFT 应该在"首个非 message.done chunk"触发；如果整个流只有 message.done
    一个 chunk，按设计 TTFT 不会触发（因为 message.done 是终态汇总，不算"首字符到达"）。
    """
    chunks = [
        LLMStreamChunk(
            kind="message.done", message=Message.assistant(content=""), finish_reason="stop"
        ),
    ]
    sink = MemoryEventSink()
    runner = Runner(stream_enabled=True, event_sinks=[sink])
    stub = StubLLMStreamProvider()
    stub.script_chunks(chunks)

    await runner.run(
        "hi", session=InMemorySession("ttft2"), agent_spec=_spec(),
        llm=stub, tools={}, approval=RecordingApproval(),
    )

    ttft_events = sink.of_kind("llm.chunk.first")
    # 仅 message.done 时不触发 TTFT（runner 过滤了 message.done）
    assert len(ttft_events) == 0
    # 但 stream.end 应该 emit
    end_events = sink.of_kind("llm.stream.end")
    assert len(end_events) == 1
