"""unit：runner 显式 interrupt（task.cancel()）行为（interrupt-run-v0.1）。

覆盖 4 个 cancel 时机 + tool_use ↔ tool_result 配对完整性 + Result 收口。

时机：
1. LLM 调用阶段 cancel（``_drive_turns`` 第一个 ``await llm.complete``）
2. approval 阻塞阶段 cancel（``approval.decide`` 内 ``await future``）
3. 单个 tool 执行阶段 cancel（``tool.execute`` 长时间 await）
4. 同 assistant 多 tool_use，call_k 正在跑 + call_k+1..N 未起跑 → 全部占位

约束（runner 的 interrupt 契约）：
- runner 顶层吞 CancelledError，return ``Result(status="cancelled")``，**不向外 raise**
- session 末尾 ``tool_use`` 必须有对应 ``tool_result``（哪怕是 ``[interrupted]`` 占位），
  否则下次 LLM 调用会被服务端 400 拒掉（Anthropic / OpenAI 协议要求配对）
- ``Result.metadata`` 含 ``cancelled_at_turn`` / ``cancelled_tool_call_id`` / ``cancel_reason``
- emit 一条 ``run.cancelled`` event + 一条 ``run.end`` event
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
    ToolContext,
    ToolResult,
)
from core.message import Message

# ---------------------------------------------------------------------------
# stub provider / approval / tool 帮手
# ---------------------------------------------------------------------------


class _StubLLM:
    """简化 LLM stub：响应来自固定 list；可塞 ``hang=True`` 让一次调用永远 await。"""

    def __init__(self, responses: list[tuple[str | None, list[ToolCall] | None]]) -> None:
        self._responses = list(responses)
        self.complete_called = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.complete_called += 1
        if not self._responses:
            return LLMResponse(message=Message(role="assistant", content=""), finish_reason="stop")
        content, tool_calls = self._responses.pop(0)
        calls_tuple = tuple(tool_calls) if tool_calls else None
        msg = Message(role="assistant", content=content, tool_calls=calls_tuple)
        finish = "tool_calls" if calls_tuple else "stop"
        return LLMResponse(message=msg, finish_reason=finish)


class _HangingLLM:
    """LLM stub：调 complete 时永远 await（模拟流式被打断的场景）。"""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        await asyncio.Event().wait()  # 永远不 set；唯一退出方式 = CancelledError
        # 不可达
        raise RuntimeError("unreachable")


class _AllowApproval:
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(outcome="approved")


class _HangingApproval:
    """approval stub：永远 await（模拟用户没点 ack）。"""

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        await asyncio.Event().wait()
        raise RuntimeError("unreachable")


class _HangingTool:
    """Tool stub：execute 永远 await。"""

    name = "hang"
    description = "hangs forever"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    async def execute(self, prepared: PreparedToolCall, ctx: ToolContext) -> ToolResult:
        del prepared, ctx
        await asyncio.Event().wait()
        raise RuntimeError("unreachable")


class _QuickTool:
    """Tool stub：立即返回成功；用来填充 parallel 测试的"不该被起跑"位置。"""

    name = "quick"
    description = "returns immediately"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, prepared: PreparedToolCall, ctx: ToolContext) -> ToolResult:
        del prepared
        self.calls.append(ctx.call_id)
        return ToolResult(ok=True, content="quick ok")


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


def _agent_spec(tool_names: tuple[str, ...]) -> AgentSpec:
    return AgentSpec(
        name="t",
        instructions="",
        default_model="m",
        tool_names=tool_names,
        max_turns=5,
    )


def _make_runner(sink: _RecordingSink) -> Runner:
    return Runner(event_sinks=[sink])


async def _wait_until(predicate: Any, *, timeout: float = 1.0) -> None:
    """忙等到 predicate() 为真或超时（避免依赖 sleep 精确时机）。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("predicate did not become true within timeout")
        await asyncio.sleep(0.01)


def _last_tool_result_msg(session: InMemorySession) -> Message | None:
    history = session.history_sync() if hasattr(session, "history_sync") else None
    if history is None:
        # InMemorySession.history 是 async；用同步 list
        return None
    for msg in reversed(history):
        if msg.role == "tool":
            return msg
    return None


# ---------------------------------------------------------------------------
# 1. LLM 调用阶段 cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_during_llm_call_returns_cancelled_result() -> None:
    """cancel 时机 = 第一个 LLM 调用 await 阻塞中。

    runner 没有任何 tool_use 被发出，所以**不需要**占位 tool_result；
    只验证 Result.status="cancelled" + 顶层不 raise + emit run.cancelled。
    """
    sink = _RecordingSink()
    session = InMemorySession("s-llm-cancel")
    llm = _HangingLLM()
    runner = _make_runner(sink)

    async def _go() -> Any:
        return await runner.run(
            "hi",
            session=session,
            agent_spec=_agent_spec(tool_names=()),
            llm=llm,
            tools={},
            approval=_AllowApproval(),
        )

    task: asyncio.Task[Any] = asyncio.create_task(_go())
    await asyncio.sleep(0.05)  # 让 runner 走到 _HangingLLM.complete
    task.cancel()
    result = await task  # runner 应该正常返回 Result，不抛 CancelledError

    assert result.status == "cancelled"
    assert result.error is None
    assert result.metadata["cancel_reason"] == "user_interrupt"
    # LLM 阶段被打断，没进入 tool，cancelled_tool_call_id = None
    assert result.metadata["cancelled_tool_call_id"] is None

    kinds = [e.kind for e in sink.events]
    assert "run.cancelled" in kinds
    assert kinds[-1] == "run.end"  # run.end 必须最后 emit


# ---------------------------------------------------------------------------
# 2. approval 阻塞阶段 cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_during_approval_writes_placeholder_tool_result() -> None:
    """cancel 时机 = approval.decide() 永远 await。

    必须给当前 call 写占位 tool_result（保证 tool_use ↔ tool_result 配对）。
    """
    sink = _RecordingSink()
    session = InMemorySession("s-approval-cancel")
    llm = _StubLLM(
        [
            (None, [ToolCall(call_id="c1", tool_name="hang", arguments={})]),
            ("done", None),  # 不会跑到，approval 阻塞
        ]
    )
    runner = _make_runner(sink)

    async def _go() -> Any:
        return await runner.run(
            "hi",
            session=session,
            agent_spec=_agent_spec(tool_names=("hang",)),
            llm=llm,
            tools={"hang": _HangingTool()},
            approval=_HangingApproval(),
        )

    task: asyncio.Task[Any] = asyncio.create_task(_go())
    # 等到 approval.request event 出现，说明走到了 approval 阻塞点
    await _wait_until(
        lambda: any(e.kind == "approval.request" for e in sink.events),
        timeout=1.0,
    )
    task.cancel()
    result = await task

    assert result.status == "cancelled"
    assert result.metadata["cancelled_tool_call_id"] == "c1"
    assert result.metadata["cancel_reason"] == "user_interrupt"

    # 配对完整：session 末尾必须有 tool_result for c1
    history = await session.history()
    tool_results = [m for m in history if m.role == "tool" and m.tool_call_id == "c1"]
    assert len(tool_results) == 1
    # 占位标记
    assert tool_results[0].metadata.get("interrupted") is True
    assert tool_results[0].metadata.get("interrupt_reason") == "user_interrupt"

    # emit 一条 interrupted 的 tool.call.end
    ends = [e for e in sink.events if e.kind == "tool.call.end"]
    assert len(ends) >= 1
    assert any(e.payload.get("reason") == "interrupted" for e in ends)


# ---------------------------------------------------------------------------
# 3. tool 执行阶段 cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_during_tool_execute_writes_placeholder() -> None:
    """cancel 时机 = execute_prepared_tool(tool, ) 永远 await。"""
    sink = _RecordingSink()
    session = InMemorySession("s-tool-cancel")
    llm = _StubLLM(
        [
            (None, [ToolCall(call_id="c1", tool_name="hang", arguments={})]),
            ("done", None),
        ]
    )
    runner = _make_runner(sink)

    async def _go() -> Any:
        return await runner.run(
            "hi",
            session=session,
            agent_spec=_agent_spec(tool_names=("hang",)),
            llm=llm,
            tools={"hang": _HangingTool()},
            approval=_AllowApproval(),
        )

    task: asyncio.Task[Any] = asyncio.create_task(_go())
    # 等 approval 决策完进入 tool.execute（看 approval.decision 事件）
    await _wait_until(
        lambda: any(e.kind == "approval.decision" for e in sink.events),
        timeout=1.0,
    )
    # 再等一小会让 _safe_tool_execute 进入 await
    await asyncio.sleep(0.05)
    task.cancel()
    result = await task

    assert result.status == "cancelled"
    assert result.metadata["cancelled_tool_call_id"] == "c1"

    history = await session.history()
    tool_results = [m for m in history if m.role == "tool" and m.tool_call_id == "c1"]
    assert len(tool_results) == 1
    assert tool_results[0].metadata.get("interrupted") is True


# ---------------------------------------------------------------------------
# 4. 同 assistant 多 tool_use，cancel 时 call_k 跑、call_k+1..N 未跑 → 全部占位
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_pending_tool_uses_also_get_placeholder() -> None:
    """同 assistant 消息含 3 个 tool_use：call_1 正在 hang，call_2/call_3 还没起跑。

    cancel 后：call_1 占位（reason=user_interrupt）+ call_2/call_3 也占位
    （reason=user_interrupt_pending）。否则下次 LLM 调用会因为
    "tool_use 多于 tool_result" 被 400 拒掉。
    """
    sink = _RecordingSink()
    session = InMemorySession("s-multi-cancel")
    llm = _StubLLM(
        [
            (
                None,
                [
                    ToolCall(call_id="c1", tool_name="hang", arguments={}),
                    ToolCall(call_id="c2", tool_name="quick", arguments={}),
                    ToolCall(call_id="c3", tool_name="quick", arguments={}),
                ],
            ),
            ("done", None),
        ]
    )
    quick = _QuickTool()
    runner = _make_runner(sink)

    async def _go() -> Any:
        return await runner.run(
            "hi",
            session=session,
            agent_spec=_agent_spec(tool_names=("hang", "quick")),
            llm=llm,
            tools={"hang": _HangingTool(), "quick": quick},
            approval=_AllowApproval(),
        )

    task: asyncio.Task[Any] = asyncio.create_task(_go())
    # 等 c1 走到 hang.execute
    await _wait_until(
        lambda: any(
            e.kind == "tool.call.start" and e.payload.get("call_id") == "c1" for e in sink.events
        ),
        timeout=1.0,
    )
    await asyncio.sleep(0.05)
    task.cancel()
    result = await task

    assert result.status == "cancelled"
    assert result.metadata["cancelled_tool_call_id"] == "c1"

    history = await session.history()
    # 3 个 tool_use 都必须配对 tool_result（即使 c2/c3 没真跑）
    paired_ids = {m.tool_call_id for m in history if m.role == "tool"}
    assert {"c1", "c2", "c3"}.issubset(paired_ids)

    # c1: reason=user_interrupt；c2/c3: reason=user_interrupt_pending
    by_id = {m.tool_call_id: m for m in history if m.role == "tool"}
    assert by_id["c1"].metadata.get("interrupt_reason") == "user_interrupt"
    assert by_id["c2"].metadata.get("interrupt_reason") == "user_interrupt_pending"
    assert by_id["c3"].metadata.get("interrupt_reason") == "user_interrupt_pending"

    # quick tool 不应被调（c2/c3 在 cancel 时尚未起跑）
    assert quick.calls == []


# ---------------------------------------------------------------------------
# 5. 跑完一个 tool 之后再 cancel：已配对的 call 不应被改成占位
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_after_one_call_completes_keeps_real_result() -> None:
    """assistant 含 [quick, hang]：quick 跑完写真实 tool_result，hang 被 cancel 写占位。

    验证：占位逻辑不会回头覆盖已经真实成功的 tool_result。
    """
    sink = _RecordingSink()
    session = InMemorySession("s-mixed-cancel")
    llm = _StubLLM(
        [
            (
                None,
                [
                    ToolCall(call_id="c1", tool_name="quick", arguments={}),
                    ToolCall(call_id="c2", tool_name="hang", arguments={}),
                ],
            ),
            ("done", None),
        ]
    )
    quick = _QuickTool()
    runner = _make_runner(sink)

    async def _go() -> Any:
        return await runner.run(
            "hi",
            session=session,
            agent_spec=_agent_spec(tool_names=("quick", "hang")),
            llm=llm,
            tools={"quick": quick, "hang": _HangingTool()},
            approval=_AllowApproval(),
        )

    task: asyncio.Task[Any] = asyncio.create_task(_go())
    await _wait_until(
        lambda: any(
            e.kind == "tool.call.start" and e.payload.get("call_id") == "c2" for e in sink.events
        ),
        timeout=1.0,
    )
    await asyncio.sleep(0.05)
    task.cancel()
    result = await task

    assert result.status == "cancelled"
    assert result.metadata["cancelled_tool_call_id"] == "c2"

    history = await session.history()
    by_id = {m.tool_call_id: m for m in history if m.role == "tool"}
    # c1 真实成功：metadata.ok=True、无 interrupted 标记
    assert by_id["c1"].metadata.get("ok") is True
    assert "interrupted" not in by_id["c1"].metadata
    # c2 占位：interrupted=True
    assert by_id["c2"].metadata.get("interrupted") is True

    # quick 应被调过一次
    assert quick.calls == ["c1"]
