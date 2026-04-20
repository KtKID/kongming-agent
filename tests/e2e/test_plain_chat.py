"""e2e P0-1：纯文本主链路。

覆盖 plan.md "e2e 主链路基线"的第一条：

> CLI 输入一句普通请求，不触发 tool，系统成功完成文本响应。

用 :class:`core.Runner` 直接装配，注入 ``StubLLMProvider`` 模拟一次无 tool_call
的响应；断言 ``Result.status == "completed"``、最终消息内容正确、事件流包含
``run.start / run.end``、approval 未被触发。
"""

from __future__ import annotations

import pytest

from core import AgentSpec, InMemorySession, Runner
from tests.e2e.conftest import MemoryEventSink, RecordingApproval, StubLLMProvider


@pytest.mark.e2e
async def test_plain_chat_returns_final_message(
    stub_llm: StubLLMProvider,
    memory_sink: MemoryEventSink,
    recording_approval: RecordingApproval,
) -> None:
    """最小纯文本一轮：用户问 hi，模型回答 '你好，我是 kongming。'。"""
    stub_llm.script(content="你好，我是 kongming。")

    runner = Runner(event_sinks=[memory_sink])
    session = InMemorySession("test-plain")
    spec = AgentSpec(
        name="test",
        instructions="You are kongming test.",
        default_model="stub",
        tool_names=(),
        max_turns=3,
    )

    result = await runner.run(
        "hello",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )

    assert result.status == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "你好，我是 kongming。"
    assert result.turn_count == 1

    # 事件流里有 run.start / run.end / turn.start / turn.end
    kinds = memory_sink.kinds()
    assert "run.start" in kinds
    assert "run.end" in kinds
    assert "turn.start" in kinds
    assert "turn.end" in kinds

    # 没有 tool call，也没有 approval
    assert "tool.call.start" not in kinds
    assert "approval.request" not in kinds
    assert recording_approval.requests == []


@pytest.mark.e2e
async def test_plain_chat_injects_system_prompt(
    stub_llm: StubLLMProvider,
    recording_approval: RecordingApproval,
) -> None:
    """AgentSpec.instructions 应该被注入为 session 开头的 system 消息。"""
    stub_llm.script(content="ok")

    runner = Runner()
    session = InMemorySession("test-sys")
    spec = AgentSpec(
        name="t",
        instructions="SYS-PROMPT",
        default_model="stub",
        max_turns=3,
    )

    await runner.run(
        "hi",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )

    history = await session.history()
    assert history[0].role == "system"
    assert history[0].content == "SYS-PROMPT"
    assert history[1].role == "user"
    assert history[1].content == "hi"
    # assistant 结束消息存在
    assert any(m.role == "assistant" and m.content == "ok" for m in history)


@pytest.mark.e2e
async def test_plain_chat_llm_received_expected_request(
    stub_llm: StubLLMProvider,
    recording_approval: RecordingApproval,
) -> None:
    """provider 应该拿到携带完整 history 的 LLMRequest。"""
    stub_llm.script(content="done")

    runner = Runner()
    session = InMemorySession("test-req")
    spec = AgentSpec(
        name="t",
        instructions="sys",
        default_model="my-model",
        max_turns=2,
    )

    await runner.run(
        "ping",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )

    assert len(stub_llm.calls) == 1
    req = stub_llm.calls[0]
    assert req.model == "my-model"
    # messages 至少包含 system + user
    roles = [m.role for m in req.messages]
    assert "system" in roles
    assert "user" in roles
