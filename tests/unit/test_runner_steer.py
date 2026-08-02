"""unit：runner turn 边界 steer 注入（cli-mailbox-steer-send #1）。

覆盖 Runner.steer / _drain_steer_buffer / 收尾吐回 五个场景：

1. run 进行中 steer → 下一 turn LLM 请求 messages 含注入的 user 消息（门控 stub LLM）
2. 无活跃 run 时 steer() 返回 False（buffer 未注册）
3. run 已进入最后 turn 不再 drain → 残留经 Result.metadata["steer_undelivered"] 吐回
4. cancelled 路径：run 被 cancel，残留同样出现在 metadata（约束16：cancel 收口成 Result）
5. steer.injected 事件被 emit（记录型 EventSink 断言）

约束：
- turn 推进/注入逻辑只在 Runner（约束3）；测试只经公开 Runner.steer + run 入口驱动
- 验证写成可重复跑的 pytest 文件（约束13），不用 python -c
"""

from __future__ import annotations

import asyncio
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
    SteerRequest,
    ToolContext,
    ToolResult,
)
from core.message import Message

# ---------------------------------------------------------------------------
# stub provider / approval / tool 帮手
# ---------------------------------------------------------------------------


class _GatedLLM:
    """门控 stub LLM。

    第一次 ``complete`` 挂起等 ``gate`` event（测试代码在此期间 steer 后 set 放行），
    返回预置的第一条响应；后续调用直接返回预置响应。每次 complete 都把收到的
    ``request.messages`` 记进 ``requests``，供断言注入是否出现在下一次请求里。
    """

    def __init__(
        self,
        responses: list[tuple[str | None, list[ToolCall] | None]],
        *,
        gate: asyncio.Event,
    ) -> None:
        self._responses = list(responses)
        self._gate = gate
        self.complete_called = 0
        self.requests: list[tuple[Message, ...]] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.complete_called += 1
        self.requests.append(tuple(request.messages))
        if self.complete_called == 1:
            # 第一次挂起，让测试代码有机会在 run 进行中调 steer。
            await self._gate.wait()
        if self._responses:
            content, tool_calls = self._responses.pop(0)
        else:
            content, tool_calls = ("done", None)
        calls_tuple = tuple(tool_calls) if tool_calls else None
        msg = Message(role="assistant", content=content, tool_calls=calls_tuple)
        finish = "tool_calls" if calls_tuple else "stop"
        return LLMResponse(message=msg, finish_reason=finish)


class _StubLLM:
    """简化 stub LLM：响应来自固定 list，用完后返回终止 stop。"""

    def __init__(self, responses: list[tuple[str | None, list[ToolCall] | None]]) -> None:
        self._responses = list(responses)
        self.complete_called = 0
        self.requests: list[tuple[Message, ...]] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.complete_called += 1
        self.requests.append(tuple(request.messages))
        if self._responses:
            content, tool_calls = self._responses.pop(0)
        else:
            content, tool_calls = ("done", None)
        calls_tuple = tuple(tool_calls) if tool_calls else None
        msg = Message(role="assistant", content=content, tool_calls=calls_tuple)
        finish = "tool_calls" if calls_tuple else "stop"
        return LLMResponse(message=msg, finish_reason=finish)


class _HangingLLM:
    """LLM stub：complete 永远 await；唯一退出方式 = CancelledError。"""

    def __init__(self) -> None:
        self.complete_called = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.complete_called += 1
        await asyncio.Event().wait()
        raise RuntimeError("unreachable")


class _AllowApproval:
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(outcome="approved")


class _QuickTool:
    """Tool stub：立即返回成功。用于制造第二个 turn（tool_call → 继续循环）。"""

    name = "quick"
    description = "returns immediately"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    async def execute(self, prepared: PreparedToolCall, ctx: ToolContext) -> ToolResult:
        del prepared, ctx
        return ToolResult(ok=True, content="quick ok")


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


def _agent_spec(tool_names: tuple[str, ...] = ()) -> AgentSpec:
    return AgentSpec(
        name="t",
        instructions="",
        default_model="m",
        tool_names=tool_names,
        max_turns=5,
    )


async def _wait_until(predicate: Any, *, timeout: float = 1.0) -> None:
    """忙等到 predicate() 为真或超时（不依赖 sleep 精确时机）。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("predicate did not become true within timeout")
        await asyncio.sleep(0.01)


def _user_texts(messages: tuple[Message, ...]) -> list[str]:
    return [m.content for m in messages if m.role == "user"]


# ---------------------------------------------------------------------------
# 1. run 进行中 steer → 下一 turn LLM 请求含注入消息
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steer_during_run_injects_into_next_turn_request() -> None:
    """run 进行中 steer，下一 turn 的 LLM 请求 messages 必须含注入的 user 文本。

    构造：turn1 complete 挂在 gate → 测试 steer("补充输入") → set gate → turn1
    返回 quick 的 tool_call → turn2 开头 drain 注入 → turn2 complete 断言 messages。
    """
    sink = _RecordingSink()
    session = InMemorySession("s-steer-inject")
    gate = asyncio.Event()
    llm = _GatedLLM(
        [
            (None, [ToolCall(call_id="c1", tool_name="quick", arguments={})]),
            ("done", None),
        ],
        gate=gate,
    )
    runner = Runner(event_sinks=[sink])

    async def _go() -> Any:
        return await runner.run(
            "hi",
            session=session,
            agent_spec=_agent_spec(tool_names=("quick",)),
            llm=llm,
            tools={"quick": _QuickTool()},
            approval=_AllowApproval(),
        )

    task: asyncio.Task[Any] = asyncio.create_task(_go())
    # 等 run 走进第一次 complete（挂在 gate 上）
    await _wait_until(lambda: llm.complete_called >= 1, timeout=1.0)
    # run 进行中：steer 命中活跃 run
    assert runner.steer("s-steer-inject", SteerRequest(text="补充输入 A")) is True
    gate.set()  # 放行第一次 complete
    result = await task

    assert result.status == "completed"
    # 第二次 complete 的 messages 必须含注入文本（drain 发生在 turn2 开头）
    assert llm.complete_called >= 2
    second_user_texts = _user_texts(llm.requests[1])
    # runner drain 注入的就是 SteerRequest.text 原文，整条 user message content == 原文
    assert "补充输入 A" in second_user_texts
    history = await session.history()
    tool_result_index = next(
        index for index, message in enumerate(history) if message.role == "tool"
    )
    injected_index = next(
        index
        for index, message in enumerate(history)
        if message.role == "user" and message.content == "补充输入 A"
    )
    assert tool_result_index < injected_index
    # 收尾无残留（已在 turn2 drain 掉）
    assert "steer_undelivered" not in result.metadata


# ---------------------------------------------------------------------------
# 2. 无活跃 run 时 steer 返回 False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steer_without_active_run_returns_false() -> None:
    """无活跃 run（buffer 未注册）时 steer 返回 False，调用方应回落排队。"""
    runner = Runner()
    assert runner.steer("nonexistent-session", SteerRequest(text="文本")) is False


# ---------------------------------------------------------------------------
# 3. run 已进入最后 turn 不再 drain → 残留吐回 metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steer_leftover_reported_in_result_metadata() -> None:
    """run 命中最后一个 turn 后 steer，来不及 drain → steer_undelivered 收残留。

    构造：turn1 complete 挂 gate（且 turn1 无 tool_call 直接 stop）→ 测试 steer →
    set gate → turn1 是终态直接结束，不再有 turn2 drain → 残留吐回 metadata。
    """
    session = InMemorySession("s-steer-leftover")
    gate = asyncio.Event()
    llm = _GatedLLM([("done", None)], gate=gate)  # turn1 直接 stop，无第二 turn
    runner = Runner()

    async def _go() -> Any:
        return await runner.run(
            "hi",
            session=session,
            agent_spec=_agent_spec(),
            llm=llm,
            tools={},
            approval=_AllowApproval(),
        )

    task: asyncio.Task[Any] = asyncio.create_task(_go())
    await _wait_until(lambda: llm.complete_called >= 1, timeout=1.0)
    assert (
        runner.steer(
            "s-steer-leftover",
            SteerRequest(text="来不及注入的文本", pending_input_id="pin-leftover-1"),
        )
        is True
    )
    gate.set()
    result = await task

    assert result.status == "completed"
    assert result.metadata["steer_undelivered"] == [
        {"text": "来不及注入的文本", "pending_input_id": "pin-leftover-1"}
    ]
    # 收尾后 buffer 已 close，再 steer 拒收
    assert runner.steer("s-steer-leftover", SteerRequest(text="x")) is False


# ---------------------------------------------------------------------------
# 4. cancelled 路径：残留同样出现在 metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steer_leftover_on_cancelled_run() -> None:
    """run 被 cancel（约束16 收口成 Result，不 raise），残留仍写进 metadata。"""
    session = InMemorySession("s-steer-cancel")
    llm = _HangingLLM()  # 第一次 complete 永远挂，等 cancel
    runner = Runner()

    async def _go() -> Any:
        return await runner.run(
            "hi",
            session=session,
            agent_spec=_agent_spec(),
            llm=llm,
            tools={},
            approval=_AllowApproval(),
        )

    task: asyncio.Task[Any] = asyncio.create_task(_go())
    await _wait_until(lambda: llm.complete_called >= 1, timeout=1.0)
    # run 进行中（挂在 LLM 调用）steer 命中
    assert (
        runner.steer(
            "s-steer-cancel",
            SteerRequest(text="cancel 前的残留", pending_input_id="pin-cancel-1"),
        )
        is True
    )
    task.cancel()
    result = await task

    assert result.status == "cancelled"
    assert result.error is None
    assert result.metadata["steer_undelivered"] == [
        {"text": "cancel 前的残留", "pending_input_id": "pin-cancel-1"}
    ]
    # cancel 收口的 metadata 字段仍在（不被 steer 收尾覆盖）
    assert result.metadata["cancel_reason"] == "user_interrupt"


# ---------------------------------------------------------------------------
# 5. steer.injected 事件被 emit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steer_injected_event_emitted() -> None:
    """drain 注入时 emit steer.injected 事件，payload 带 pending_input_id + content_length。"""
    sink = _RecordingSink()
    session = InMemorySession("s-steer-event")
    gate = asyncio.Event()
    llm = _GatedLLM(
        [
            (None, [ToolCall(call_id="c1", tool_name="quick", arguments={})]),
            ("done", None),
        ],
        gate=gate,
    )
    runner = Runner(event_sinks=[sink])

    async def _go() -> Any:
        return await runner.run(
            "hi",
            session=session,
            agent_spec=_agent_spec(tool_names=("quick",)),
            llm=llm,
            tools={"quick": _QuickTool()},
            approval=_AllowApproval(),
        )

    task: asyncio.Task[Any] = asyncio.create_task(_go())
    await _wait_until(lambda: llm.complete_called >= 1, timeout=1.0)
    text = "触发事件的补充输入"
    assert (
        runner.steer(
            "s-steer-event",
            SteerRequest(text=text, pending_input_id="pin-event-1"),
        )
        is True
    )
    gate.set()
    result = await task

    assert result.status == "completed"
    injected = [e for e in sink.events if e.kind == "steer.injected"]
    assert len(injected) == 1
    # pending_input_id 是消账主键；content_length 保留为纯观测字段
    assert injected[0].payload["pending_input_id"] == "pin-event-1"
    assert injected[0].payload["content_length"] == len(text)
    assert injected[0].run_id == result.run_id


@pytest.mark.asyncio
async def test_steer_injected_event_carries_none_when_request_has_no_id() -> None:
    """steer 请求缺 pending_input_id 时，emit 事件 payload 的 id 字段为 None。

    覆盖外部直调（非 Web send-now）路径：SteerRequest 只传 text，id 留 None。
    Runner 仍正常 emit，下游消费方据 None 决定不消账。
    """
    sink = _RecordingSink()
    session = InMemorySession("s-steer-no-id")
    gate = asyncio.Event()
    llm = _GatedLLM(
        [
            (None, [ToolCall(call_id="c1", tool_name="quick", arguments={})]),
            ("done", None),
        ],
        gate=gate,
    )
    runner = Runner(event_sinks=[sink])

    async def _go() -> Any:
        return await runner.run(
            "hi",
            session=session,
            agent_spec=_agent_spec(tool_names=("quick",)),
            llm=llm,
            tools={"quick": _QuickTool()},
            approval=_AllowApproval(),
        )

    task: asyncio.Task[Any] = asyncio.create_task(_go())
    await _wait_until(lambda: llm.complete_called >= 1, timeout=1.0)
    assert runner.steer("s-steer-no-id", SteerRequest(text="无 id 的 steer")) is True
    gate.set()
    result = await task

    assert result.status == "completed"
    injected = [e for e in sink.events if e.kind == "steer.injected"]
    assert len(injected) == 1
    assert injected[0].payload["pending_input_id"] is None
