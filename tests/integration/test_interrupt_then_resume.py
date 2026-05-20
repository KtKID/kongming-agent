"""集成：被打断的 thread 下一轮 user.input 不会被 LLM 服务端 400 拒掉
（interrupt-run-v0.1 #13）。

回归保护 thread-6bb3d9e1 类型 bug：那次现场是同 thread 并发跑多 run，
session jsonl 里残留 ``tool_use`` 没对应 ``tool_result``，Anthropic provider
按配对规则发出去被 400 拒。本测试不开启并发，但模拟"用户主动 interrupt
后立即追发"，验证 runner 的 ``_finalize_unpaired_call`` 占位机制让历史
保持配对完整，下一轮 LLM 消息体可被 provider 正常接受。

不用 mock provider 服务端：只用 stub LLM 验证传入它的 messages 结构合法
（assistant 含 tool_use → 紧跟 tool_result，且全部 tool_use 配对）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from core import AgentSpec, InMemorySession, Runner, ToolCall
from core.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    LLMRequest,
    LLMResponse,
    ToolContext,
    ToolResult,
)
from core.message import Message


class _RecordingLLM:
    """记录每次 ``complete`` 收到的 messages，便于验证配对完整性。

    脚本化响应：第一轮发 tool_use（c1, c2），第二轮（resume 后）回普通文本。
    """

    def __init__(self) -> None:
        self.received_messages: list[tuple[Message, ...]] = []
        self._turn = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.received_messages.append(request.messages)
        self._turn += 1
        if self._turn == 1:
            calls = (
                ToolCall(call_id="c1", tool_name="hang", arguments={}),
                ToolCall(call_id="c2", tool_name="quick", arguments={}),
            )
            return LLMResponse(
                message=Message(role="assistant", content=None, tool_calls=calls),
                finish_reason="tool_calls",
            )
        # resume 后：纯文本
        return LLMResponse(
            message=Message(role="assistant", content="resumed ok"),
            finish_reason="stop",
        )


class _Allow:
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(outcome="approved")


class _HangingTool:
    name = "hang"
    description = "hang"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        await asyncio.Event().wait()
        raise RuntimeError("unreachable")


class _QuickTool:
    name = "quick"
    description = "quick"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, content="quick ok")


def _assert_tool_use_pairs_complete(messages: tuple[Message, ...]) -> None:
    """断言所有 assistant.tool_calls 都有紧随其后的 tool 消息对应 tool_call_id。

    Anthropic / OpenAI 协议要求：一条 assistant 消息里所有 tool_use 必须
    有对应的 tool_result 消息（按 tool_call_id 配对）。
    """
    pending_call_ids: set[str] = set()
    for msg in messages:
        if msg.role == "assistant" and msg.tool_calls:
            pending_call_ids.update(c.call_id for c in msg.tool_calls)
        elif msg.role == "tool" and msg.tool_call_id:
            pending_call_ids.discard(msg.tool_call_id)
    assert not pending_call_ids, (
        f"unpaired tool_use call_ids 漏掉 tool_result: {sorted(pending_call_ids)}；"
        f"这是会被 LLM provider 400 拒掉的场景"
    )


@pytest.mark.asyncio
async def test_interrupt_then_resume_keeps_history_pairing_complete() -> None:
    """场景：第一次 run 在 c1（hang）执行阶段被 cancel → 第二次 run 接同 session。

    验证：
    1. 第一次 run.status == "cancelled"
    2. session 里 c1/c2 都有 tool_result（c1 = user_interrupt，c2 = user_interrupt_pending）
    3. 第二次 run 的 LLM 收到的 messages 全部 tool_use ↔ tool_result 配对完整
    4. 第二次 run.status == "completed"，不会触发任何 LLM provider 400 类错误
    """
    session = InMemorySession("s-resume")
    llm = _RecordingLLM()
    runner = Runner()

    # 第 1 轮：发出去会被 hang
    async def _go1() -> Any:
        return await runner.run(
            "first",
            session=session,
            agent_spec=AgentSpec(
                name="t",
                instructions="",
                default_model="m",
                tool_names=("hang", "quick"),
                max_turns=5,
            ),
            llm=llm,
            tools={"hang": _HangingTool(), "quick": _QuickTool()},
            approval=_Allow(),
        )

    task1: asyncio.Task[Any] = asyncio.create_task(_go1())
    # 等到 LLM 第一次 complete 被调（即将进入 hang.execute）
    deadline = asyncio.get_event_loop().time() + 1.0
    while llm._turn == 0 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.01)
    # 让 hang.execute 进入 await
    await asyncio.sleep(0.05)

    task1.cancel()
    result1 = await task1

    assert result1.status == "cancelled"
    assert result1.metadata["cancelled_tool_call_id"] == "c1"

    # session 检查：c1/c2 都已配对（c2 是未起跑也占位的）
    history = await session.history()
    paired = {m.tool_call_id for m in history if m.role == "tool" and m.tool_call_id}
    assert {"c1", "c2"}.issubset(paired)
    _assert_tool_use_pairs_complete(tuple(history))

    # 第 2 轮 resume：跑同一个 runner + 同一 session（模拟用户追发）
    result2 = await runner.run(
        "second (resume)",
        session=session,
        agent_spec=AgentSpec(
            name="t",
            instructions="",
            default_model="m",
            tool_names=("hang", "quick"),
            max_turns=5,
        ),
        llm=llm,
        tools={"hang": _HangingTool(), "quick": _QuickTool()},
        approval=_Allow(),
    )

    assert result2.status == "completed"
    # 第二次 LLM 收到的 messages 必须全部配对完整
    # （否则真实 Anthropic provider 会 400 拒）
    assert len(llm.received_messages) >= 2
    second_call_msgs = llm.received_messages[-1]
    _assert_tool_use_pairs_complete(second_call_msgs)
    # 且 second_call_msgs 至少包含上一轮的 c1/c2 占位
    paired_in_request = {
        m.tool_call_id for m in second_call_msgs if m.role == "tool" and m.tool_call_id
    }
    assert {"c1", "c2"}.issubset(paired_in_request)
