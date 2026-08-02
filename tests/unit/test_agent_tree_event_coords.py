"""agent-tree-v0.1 task-1 模块 G/H：Event 坐标字段 + 统一时间工具 + wire 漂移。

覆盖 DoD：
- DoD-1：clock.now_epoch_ms / now_iso tz-aware 来源
- DoD-2/DoD-3：Event 三坐标字段默认值兼容；timestamp_ms 走 now_epoch_ms
- DoD-4：ToolContext.agent_id 默认值兼容；runner 透传 agent_id 到 Event/ctx
- DoD-6：wire 双侧同步——_S2CFrameBase.agent_id 默认值 + WSEventSink 透传
  Event.agent_id 到帧 + round-trip 不丢字段（漂移测试）

设计要点：
- 全部走真实类型构造 + model_dump/model_validate（不 mock 协议层）。
- WSEventSink 用 fake ws 收帧，断言翻译出的帧携带 Event.agent_id。
- runner 透传测试用最小 stub LLM + 记录 sink，断言 Event.agent_id == 传入值。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from core import AgentSpec, InMemorySession, Runner
from core.clock import now_epoch_ms, now_iso
from core.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    Event,
    LLMRequest,
    LLMResponse,
    ToolContext,
)
from core.message import Message
from hosts.web.protocol.ws_frames import (
    ContentDeltaFrame,
    TurnStartFrame,
)
from hosts.web.websocket.event_sink import WSEventSink

# ---------------------------------------------------------------------------
# DoD-1：统一时间工具 tz-aware
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_now_epoch_ms_returns_positive_int() -> None:
    """now_epoch_ms 返回正整数（epoch 毫秒）。"""
    ts = now_epoch_ms()
    assert isinstance(ts, int)
    # 2026 年 epoch ms 远大于 1.7e12，确保不是 0 / 负数
    assert ts > 1_700_000_000_000


@pytest.mark.unit
def test_now_epoch_ms_is_monotonic_non_decreasing() -> None:
    """连续两次调用，后者不小于前者（tz-aware 单一来源）。"""
    a = now_epoch_ms()
    b = now_epoch_ms()
    assert b >= a


@pytest.mark.unit
def test_now_iso_is_iso8601_with_timezone() -> None:
    """now_iso 返回带时区的 ISO8601 字符串，可被 datetime.fromisoformat 解析。"""
    s = now_iso()
    assert isinstance(s, str)
    # datetime.fromisoformat 在 3.11+ 能解析带 +00:00 的串
    dt = datetime.fromisoformat(s)
    # tz-aware：utcoffset 非 None
    assert dt.utcoffset() is not None
    # 与 UTC 一致
    assert dt.utcoffset() == datetime.now(UTC).utcoffset()


# ---------------------------------------------------------------------------
# DoD-2 / DoD-3：Event 三坐标字段默认值兼容 + timestamp_ms 走 now_epoch_ms
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_event_default_fields_compatible() -> None:
    """Event(kind, run_id) 不抛；坐标字段默认 ""；timestamp_ms 是正 int。"""
    e = Event(kind="run.start", run_id="r-1")
    assert e.agent_id == ""
    assert e.task_id == ""
    assert e.conversation_id == ""
    assert isinstance(e.timestamp_ms, int)
    assert e.timestamp_ms > 0


@pytest.mark.unit
def test_event_explicit_coords_preserved() -> None:
    """显式传入的坐标字段被保留。"""
    e = Event(
        kind="turn.start",
        run_id="r-1",
        agent_id="agent-abc",
        task_id="task-xyz",
        conversation_id="thread-1",
    )
    assert e.agent_id == "agent-abc"
    assert e.task_id == "task-xyz"
    assert e.conversation_id == "thread-1"


# ---------------------------------------------------------------------------
# DoD-4：ToolContext.agent_id 默认值兼容
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_context_default_agent_id_compatible() -> None:
    """ToolContext 不传 agent_id 时不抛；默认 ""。"""
    ctx = ToolContext(run_id="r", session_id="s", turn=1, call_id="c")
    assert ctx.agent_id == ""


@pytest.mark.unit
def test_tool_context_explicit_agent_id_preserved() -> None:
    """ToolContext 显式 agent_id 被保留。"""
    ctx = ToolContext(run_id="r", session_id="s", turn=1, call_id="c", agent_id="agent-abc")
    assert ctx.agent_id == "agent-abc"


# ---------------------------------------------------------------------------
# DoD-6：wire 双侧同步——_S2CFrameBase.agent_id + WSEventSink 透传（漂移测试）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_s2c_frame_base_has_agent_id_default_empty() -> None:
    """所有 S2C 帧继承 _S2CFrameBase，默认 agent_id=''，构造时不传也不报错。"""
    frame = TurnStartFrame(timestamp_ms=1, turn=1)
    assert frame.agent_id == ""
    # model_dump 包含 agent_id 字段（wire 透传）
    dumped = frame.model_dump()
    assert dumped["agent_id"] == ""


@pytest.mark.unit
def test_s2c_frame_agent_id_round_trip() -> None:
    """S2C 帧 agent_id 经 JSON round-trip 不丢（漂移测试核心断言）。"""
    original = ContentDeltaFrame(
        timestamp_ms=1_700_000_000_003,
        delta="he",
        turn=1,
        seq=0,
        agent_id="agent-xyz",
    )
    blob = original.model_dump_json()
    reconstructed = ContentDeltaFrame.model_validate_json(blob)
    assert reconstructed == original
    assert reconstructed.agent_id == "agent-xyz"


@pytest.mark.unit
async def test_ws_event_sink_translates_event_agent_id_to_frame() -> None:
    """WSEventSink._translate 透传 Event.agent_id 到 wire 帧的 agent_id。

    漂移测试：runtime Event → WS 帧翻译链路保留坐标字段（约束 17 双侧一致）。
    """

    class _FakeWS:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def send_json(self, payload: dict[str, Any]) -> None:
            self.sent.append(payload)

    ws = _FakeWS()
    sink = WSEventSink(ws, thread_id="thread-1")
    # 构造带 agent_id 坐标的 turn.start Event
    event = Event(
        kind="turn.start",
        run_id="run-1",
        turn=1,
        agent_id="agent-abc",
    )
    await sink.emit(event)
    assert len(ws.sent) == 1
    frame = ws.sent[0]
    # 翻译出的 TurnStartFrame 携带 agent_id 坐标
    assert frame["frame_type"] == "turn.start"
    assert frame["agent_id"] == "agent-abc"


@pytest.mark.unit
async def test_ws_event_sink_empty_agent_id_yields_empty_frame_field() -> None:
    """Event.agent_id 为空时，翻译出的帧 agent_id 也是 ""（不丢字段）。"""

    class _FakeWS:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def send_json(self, payload: dict[str, Any]) -> None:
            self.sent.append(payload)

    ws = _FakeWS()
    sink = WSEventSink(ws, thread_id="thread-1")
    event = Event(kind="turn.start", run_id="run-1", turn=1)
    await sink.emit(event)
    assert len(ws.sent) == 1
    assert ws.sent[0]["agent_id"] == ""


# ---------------------------------------------------------------------------
# runner 透传：agent_id 经 run() 参数 → Event.agent_id + ToolContext.agent_id
# ---------------------------------------------------------------------------


class _StubLLM:
    """单轮返回纯文本的 stub LLM，避免 unit 层 import e2e/conftest。"""

    def __init__(self) -> None:
        self.calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        return LLMResponse(
            message=Message(role="assistant", content="done"),
            finish_reason="stop",
        )


class _AllowApproval:
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(outcome="approved")


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


@pytest.mark.unit
async def test_runner_propagates_agent_id_to_events() -> None:
    """runner.run(agent_id=...) 的所有 Event 都带该 agent_id 坐标。"""
    llm = _StubLLM()
    runner = Runner()
    session = InMemorySession("u")
    spec = AgentSpec(name="t", instructions="s", default_model="m", max_turns=3)
    sink = _RecordingSink()

    await runner.run(
        "hi",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={},
        approval=_AllowApproval(),
        event_sinks=[sink],
        agent_id="agent-main",
    )

    assert len(sink.events) > 0
    # 所有 Event 的 agent_id 坐标都被注入
    for ev in sink.events:
        assert ev.agent_id == "agent-main"


@pytest.mark.unit
async def test_runner_default_agent_id_is_empty() -> None:
    """不传 agent_id 时（单 agent 兼容场景），Event.agent_id 为 ""。"""
    llm = _StubLLM()
    runner = Runner()
    session = InMemorySession("u")
    spec = AgentSpec(name="t", instructions="s", default_model="m", max_turns=3)
    sink = _RecordingSink()

    await runner.run(
        "hi",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={},
        approval=_AllowApproval(),
        event_sinks=[sink],
    )

    assert len(sink.events) > 0
    for ev in sink.events:
        assert ev.agent_id == ""
