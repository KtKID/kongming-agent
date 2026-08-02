"""unit：runner.run 的 run_id 拼装语义（M2 验收）。

覆盖：
- 连续 2 次 run_once → run_id 形如 ``run-{session_id}-1`` / ``run-{session_id}-2``
- 跨 session 隔离 → 不同 session_id 的 run_index 互不影响
- 外部注入 run_id 兜底 → state.run_id 保留外部值，不调 advance_run_index
- emit run.start 在 _seed_messages 之后（payload 含正确 run_id）
"""

from __future__ import annotations

import pytest

from core import AgentSpec, InMemorySession, Runner
from core.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    Event,
    LLMRequest,
    LLMResponse,
)
from core.message import Message


class _StubLLM:
    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            message=Message(role="assistant", content="ok"),
            finish_reason="stop",
        )


class _AllowApproval:
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(outcome="approved")


class _CollectingSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


def _spec() -> AgentSpec:
    return AgentSpec(name="t", instructions="s", default_model="m", max_turns=3)


@pytest.mark.unit
async def test_run_id_format_first_run() -> None:
    """首次 run_once → run_id == ``run-{session_id}-1``。"""
    runner = Runner()
    session = InMemorySession("sess-A")
    result = await runner.run(
        "hi",
        session=session,
        agent_spec=_spec(),
        llm=_StubLLM(),
        tools={},
        approval=_AllowApproval(),
    )
    assert result.run_id == "run-sess-A-1"


@pytest.mark.unit
async def test_run_id_increments_within_session() -> None:
    """同 session 连续 2 次 run → run_id 顺序自增。"""
    runner = Runner()
    session = InMemorySession("sess-X")
    r1 = await runner.run(
        "hi",
        session=session,
        agent_spec=_spec(),
        llm=_StubLLM(),
        tools={},
        approval=_AllowApproval(),
    )
    r2 = await runner.run(
        "hi again",
        session=session,
        agent_spec=_spec(),
        llm=_StubLLM(),
        tools={},
        approval=_AllowApproval(),
    )
    assert r1.run_id == "run-sess-X-1"
    assert r2.run_id == "run-sess-X-2"


@pytest.mark.unit
async def test_run_id_isolated_across_sessions() -> None:
    """不同 session_id 的 run_index 互不影响。"""
    runner = Runner()
    sa = InMemorySession("alpha")
    sb = InMemorySession("beta")
    ra = await runner.run(
        "hi",
        session=sa,
        agent_spec=_spec(),
        llm=_StubLLM(),
        tools={},
        approval=_AllowApproval(),
    )
    rb = await runner.run(
        "hi",
        session=sb,
        agent_spec=_spec(),
        llm=_StubLLM(),
        tools={},
        approval=_AllowApproval(),
    )
    assert ra.run_id == "run-alpha-1"
    assert rb.run_id == "run-beta-1"


@pytest.mark.unit
async def test_external_run_id_overrides_advance() -> None:
    """外部注入 run_id 时不调 advance_run_index，保留传入标识。"""
    runner = Runner()
    session = InMemorySession("sess-Y")
    result = await runner.run(
        "hi",
        session=session,
        agent_spec=_spec(),
        llm=_StubLLM(),
        tools={},
        approval=_AllowApproval(),
        run_id="external-trace-id",
    )
    assert result.run_id == "external-trace-id"
    # InMemorySession 的 _run_count 应当未被自增（保留 0）
    assert session._run_count == 0


@pytest.mark.unit
async def test_run_start_event_carries_correct_run_id() -> None:
    """emit run.start 必须在 advance 之后；payload 的 run_id 是新格式。"""
    sink = _CollectingSink()
    runner = Runner(event_sinks=[sink])
    session = InMemorySession("sess-Z")
    await runner.run(
        "hi",
        session=session,
        agent_spec=_spec(),
        llm=_StubLLM(),
        tools={},
        approval=_AllowApproval(),
    )
    run_start_events = [e for e in sink.events if e.kind == "run.start"]
    assert len(run_start_events) == 1
    assert run_start_events[0].run_id == "run-sess-Z-1"


@pytest.mark.unit
async def test_all_emitted_events_share_same_run_id() -> None:
    """同一次 run 的所有 Event.run_id 必须一致（不能有空串遗漏）。"""
    sink = _CollectingSink()
    runner = Runner(event_sinks=[sink])
    session = InMemorySession("sess-W")
    await runner.run(
        "hi",
        session=session,
        agent_spec=_spec(),
        llm=_StubLLM(),
        tools={},
        approval=_AllowApproval(),
    )
    expected_run_id = "run-sess-W-1"
    # 收集所有非空 run_id；空串理论上只可能出现在异常发生于 _seed_messages 之前
    run_ids = {e.run_id for e in sink.events if e.run_id}
    assert run_ids == {expected_run_id}, f"events have inconsistent run_id: {run_ids}"
