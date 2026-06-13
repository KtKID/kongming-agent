"""unit：core.runner happy path + max_turns 保护。

只覆盖 runner 最核心的两条单测级行为：

1. 一轮 happy path → ``Result.status == "completed"``
2. 永远发 tool_call 但没有对应 tool 时，``max_turns`` 触发
   :class:`MaxTurnsExceededError` 并被收口到 ``Result.status == "failed"``
"""

from __future__ import annotations

import pytest

from core import AgentSpec, InMemorySession, Runner, ToolCall
from core.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    AssembledInput,
    LLMRequest,
    LLMResponse,
)
from core.errors import MaxTurnsExceededError
from core.message import Message


class _StubLLM:
    """本地 stub，避免 unit 层 import e2e/conftest。"""

    def __init__(
        self,
        responses: list[tuple[str | None, list[ToolCall] | None]] | None = None,
        usage: dict | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._usage = usage or {}
        self.calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if not self._responses:
            return LLMResponse(
                message=Message(role="assistant", content=""),
                finish_reason="stop",
                usage=dict(self._usage),
            )
        content, tool_calls = self._responses.pop(0)
        calls_tuple = tuple(tool_calls) if tool_calls else None
        msg = Message(role="assistant", content=content, tool_calls=calls_tuple)
        finish = "tool_calls" if calls_tuple else "stop"
        return LLMResponse(message=msg, finish_reason=finish, usage=dict(self._usage))


class _AllowApproval:
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(outcome="approved")


class _CaptureAssembler:
    """测试用 assembler，记录输入来源并注入 system 消息。"""

    def __init__(self) -> None:
        """初始化 assembler，输入为空，输出为可记录 sources 的实例。"""
        self.sources: list[list[object]] = []

    async def assemble(
        self,
        history: list[Message],
        instructions: list[object] = (),
    ) -> AssembledInput:
        """装配消息，输入为历史和指令来源，输出含 system 的消息列表。"""
        self.sources.append(list(instructions))
        system_text = "\n".join(str(source.content) for source in instructions)
        messages = [Message.system(system_text), *history] if system_text else list(history)
        return AssembledInput(
            messages=messages,
            metadata={"original_count": len(history), "compacted_count": len(messages)},
            system_message=messages[0] if system_text else None,
        )


@pytest.mark.unit
async def test_runner_happy_path_single_turn() -> None:
    llm = _StubLLM([("hello", None)])
    runner = Runner()
    session = InMemorySession("u")
    spec = AgentSpec(name="t", instructions="s", default_model="m", max_turns=3)

    result = await runner.run(
        "hi",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={},
        approval=_AllowApproval(),
    )

    assert result.status == "completed"
    assert result.final_message is not None
    assert result.final_message.content == "hello"
    assert result.turn_count == 1
    assert result.error is None


@pytest.mark.unit
async def test_runner_assembler_uses_per_run_agent_spec_instructions() -> None:
    """同一个 runner 跑子 agent 时，assembler 必须使用本次 agent_spec 指令。"""
    llm = _StubLLM([("ok", None)])
    assembler = _CaptureAssembler()
    runner = Runner(
        input_assembler=assembler,
        instruction_sources=[type("Source", (), {"origin": "", "content": "parent tools"})()],
    )
    session = InMemorySession("child")
    spec = AgentSpec(name="child", instructions="child-only instructions", default_model="m")

    result = await runner.run(
        "task",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={},
        approval=_AllowApproval(),
    )

    assert result.status == "completed"
    assert assembler.sources
    assert getattr(assembler.sources[0][0], "content") == "child-only instructions"
    assert llm.calls[0].messages[0].content == "child-only instructions"


@pytest.mark.unit
async def test_runner_max_turns_exceeded_is_captured_in_result() -> None:
    """模型无限发 tool_call 且找不到 tool，runner 达到 max_turns 会失败收口。"""
    # 每一轮都返回一个会触发 "tool not registered" 的 tool call
    responses = [
        (None, [ToolCall(call_id=f"c{i}", tool_name="ghost", arguments={})]) for i in range(10)
    ]
    llm = _StubLLM(responses)

    runner = Runner()
    session = InMemorySession("u")
    spec = AgentSpec(name="t", instructions="", default_model="m", max_turns=2)

    result = await runner.run(
        "hi",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={},
        approval=_AllowApproval(),
    )

    assert result.status == "failed"
    assert isinstance(result.error, MaxTurnsExceededError)
    assert result.final_message is None


@pytest.mark.unit
async def test_runner_resolves_tool_names_from_spec() -> None:
    """spec.tool_names 里声明的 tool 必须能在 ToolLookup 里找到，否则直接失败。"""
    llm = _StubLLM([("ok", None)])
    runner = Runner()
    session = InMemorySession("u")
    spec = AgentSpec(
        name="t",
        instructions="",
        default_model="m",
        tool_names=("unknown_tool",),
        max_turns=3,
    )

    result = await runner.run(
        "hi",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={},
        approval=_AllowApproval(),
    )

    assert result.status == "failed"
    assert result.error is not None
    assert "unknown_tool" in result.error.message


@pytest.mark.unit
async def test_runner_usage_in_result_metadata() -> None:
    """runner 把 usage 累计写入 Result.metadata['usage']。"""
    llm = _StubLLM(
        [("hello", None)],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    runner = Runner()
    session = InMemorySession("u")
    spec = AgentSpec(name="t", instructions="s", default_model="m", max_turns=3)

    result = await runner.run(
        "hi",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={},
        approval=_AllowApproval(),
    )

    assert result.status == "completed"
    usage = result.metadata.get("usage")
    assert isinstance(usage, dict)
    assert usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 5
    assert usage["total_tokens"] == 15


@pytest.mark.unit
async def test_runner_appends_user_message_to_session() -> None:
    llm = _StubLLM([("ok", None)])
    runner = Runner()
    session = InMemorySession("u")
    spec = AgentSpec(name="t", instructions="SYS", default_model="m", max_turns=2)

    await runner.run(
        "my-input",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={},
        approval=_AllowApproval(),
    )

    history = await session.history()
    user_msgs = [m for m in history if m.role == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0].content == "my-input"
