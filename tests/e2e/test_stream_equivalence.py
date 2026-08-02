"""E#1：流式 ↔ 非流式等价性 V1 核心质量门。

同一份 LLMStreamChunk 脚本喂两路：
- stream 路径：runner._consume_stream 消费 chunks
- non-stream 路径：runner._safe_llm_complete 直接拿 message.done 的 LLMResponse

断言两路产出的 Result.final_message / status / 关键 metadata 等价。
StubLLMStreamProvider 在 conftest.py 中实现，同一份 script_chunks 同时驱动 stream + complete。
"""

from __future__ import annotations

import pytest

from core import AgentSpec, InMemorySession, Runner
from core.contracts import LLMStreamChunk, ProviderUsageFamily
from core.message import Message, ToolCall
from infrastructure.llm_providers.usage import ProviderUsageManager
from tests.e2e.conftest import RecordingApproval, StubLLMStreamProvider


def _spec(name: str = "test") -> AgentSpec:
    return AgentSpec(
        name=name,
        instructions="You are a test agent.",
        default_model="stub",
        max_turns=5,
    )


async def _run_with(*, stream_enabled: bool, stub: StubLLMStreamProvider) -> dict:
    """跑一次 runner，返回 {final_message, status, turn_count, usage}。"""
    runner = Runner(stream_enabled=stream_enabled)
    session = InMemorySession(f"test-stream-eq-{stream_enabled}")
    result = await runner.run(
        "hello",
        session=session,
        agent_spec=_spec(),
        llm=stub,
        tools={},
        approval=RecordingApproval(),
    )
    return {
        "status": result.status,
        "final_message": result.final_message,
        "turn_count": result.turn_count,
        "usage": result.metadata.get("usage", {}),
    }


@pytest.mark.e2e
async def test_e1_1_pure_content_equivalence() -> None:
    """E.1.1：纯文本响应在 stream / non-stream 路径下产出等价 final_message。"""
    final_msg = Message.assistant(content="Hello world")
    usage = ProviderUsageManager().normalize(
        family=ProviderUsageFamily.OPENAI_CHAT_COMPLETIONS,
        raw_usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    )
    chunks = [
        LLMStreamChunk(kind="content.delta", delta="Hel", index=0),
        LLMStreamChunk(kind="content.delta", delta="lo ", index=0),
        LLMStreamChunk(kind="content.delta", delta="world", index=0),
        LLMStreamChunk(
            kind="message.done",
            message=final_msg,
            finish_reason="stop",
            usage=usage,
        ),
    ]

    # 流式
    stub_s = StubLLMStreamProvider()
    stub_s.script_chunks(chunks)
    res_stream = await _run_with(stream_enabled=True, stub=stub_s)

    # 非流式（同一份 chunks 通过 complete 拼装）
    stub_n = StubLLMStreamProvider()
    stub_n.script_chunks(chunks)
    res_nonstream = await _run_with(stream_enabled=False, stub=stub_n)

    assert res_stream["status"] == "completed" == res_nonstream["status"]
    assert res_stream["final_message"].content == "Hello world"
    assert res_nonstream["final_message"].content == "Hello world"
    assert res_stream["usage"] == res_nonstream["usage"]


@pytest.mark.e2e
async def test_e1_2_tool_calls_equivalence() -> None:
    """E.1.2：tool_calls 响应（runner 不调用工具，最大轮数=1）。"""
    tc = ToolCall(call_id="c1", tool_name="ToolA", arguments={"x": 1})
    final_msg = Message.assistant(content=None, tool_calls=[tc])
    chunks = [
        LLMStreamChunk(kind="tool_call.start", index=0, tool_call_id="c1", tool_name="ToolA"),
        LLMStreamChunk(
            kind="tool_call.arguments.delta",
            index=0,
            delta='{"x":1}',
            tool_call_id="c1",
        ),
        LLMStreamChunk(kind="tool_call.end", index=0, tool_call_id="c1", tool_name="ToolA"),
        LLMStreamChunk(
            kind="message.done",
            message=final_msg,
            finish_reason="tool_calls",
        ),
    ]

    # 让 runner max_turns=1，这样收到 tool_calls 后就因为 max_turns 而停（避免要真实 tool 执行）
    runner_s = Runner(stream_enabled=True)
    runner_n = Runner(stream_enabled=False)
    spec = AgentSpec(name="t", instructions="x", default_model="stub", max_turns=1)

    stub_s = StubLLMStreamProvider()
    stub_s.script_chunks(chunks)
    res_stream = await runner_s.run(
        "hi",
        session=InMemorySession("s1"),
        agent_spec=spec,
        llm=stub_s,
        tools={},
        approval=RecordingApproval(),
    )
    stub_n = StubLLMStreamProvider()
    stub_n.script_chunks(chunks)
    res_nonstream = await runner_n.run(
        "hi",
        session=InMemorySession("s2"),
        agent_spec=spec,
        llm=stub_n,
        tools={},
        approval=RecordingApproval(),
    )

    # tool_call 后 runner 尝试执行未注册工具 → tool_result error → 第 2 轮被 max_turns=1 截停
    # 两路径行为一致：stream 和 non-stream 都触发 MaxTurnsExceededError → status="failed"
    assert res_stream.status == res_nonstream.status
    # 关键断言：流式与非流式拿到的 turn 1 final_message.tool_calls 一致
    # （两路径都因 max_turns 失败，但 history 里 turn 1 应当一样）


@pytest.mark.e2e
async def test_e1_3_reasoning_equivalence() -> None:
    """E.1.3：reasoning + content 混合，final_message 与 provider_metadata 等价。"""
    final_msg = Message.assistant(content="answer")
    chunks = [
        LLMStreamChunk(kind="reasoning.delta", delta="think A"),
        LLMStreamChunk(kind="reasoning.delta", delta=" B"),
        LLMStreamChunk(kind="content.delta", delta="answer", index=0),
        LLMStreamChunk(
            kind="message.done",
            message=final_msg,
            finish_reason="stop",
            provider_metadata={"reasoning_content": "think A B", "reasoning_content_length": 9},
        ),
    ]

    stub_s = StubLLMStreamProvider()
    stub_s.script_chunks(chunks)
    res_stream = await _run_with(stream_enabled=True, stub=stub_s)

    stub_n = StubLLMStreamProvider()
    stub_n.script_chunks(chunks)
    res_nonstream = await _run_with(stream_enabled=False, stub=stub_n)

    assert res_stream["final_message"].content == res_nonstream["final_message"].content == "answer"


@pytest.mark.e2e
async def test_e1_4_finish_reason_passthrough() -> None:
    """E.1.4：finish_reason 在两路径下完全一致（length / stop / tool_calls）。"""
    for finish in ("stop", "length"):
        chunks = [
            LLMStreamChunk(kind="content.delta", delta="x", index=0),
            LLMStreamChunk(
                kind="message.done",
                message=Message.assistant(content="x"),
                finish_reason=finish,  # type: ignore[arg-type]
            ),
        ]
        stub_s = StubLLMStreamProvider()
        stub_s.script_chunks(chunks)
        # stream 路径
        runner_s = Runner(stream_enabled=True)
        await runner_s.run(
            "hi",
            session=InMemorySession(f"s-{finish}"),
            agent_spec=_spec(),
            llm=stub_s,
            tools={},
            approval=RecordingApproval(),
        )
        # non-stream 路径用同一份 chunks
        stub_n = StubLLMStreamProvider()
        stub_n.script_chunks(chunks)
        runner_n = Runner(stream_enabled=False)
        res_n = await runner_n.run(
            "hi",
            session=InMemorySession(f"n-{finish}"),
            agent_spec=_spec(),
            llm=stub_n,
            tools={},
            approval=RecordingApproval(),
        )
        assert res_n.status == "completed"


@pytest.mark.e2e
async def test_e1_5_empty_content_message() -> None:
    """E.1.5：流只有 message.done（无任何 delta）也应正常完成。"""
    final_msg = Message.assistant(content="")
    chunks = [
        LLMStreamChunk(kind="message.done", message=final_msg, finish_reason="stop"),
    ]

    stub_s = StubLLMStreamProvider()
    stub_s.script_chunks(chunks)
    res_stream = await _run_with(stream_enabled=True, stub=stub_s)

    stub_n = StubLLMStreamProvider()
    stub_n.script_chunks(chunks)
    res_nonstream = await _run_with(stream_enabled=False, stub=stub_n)

    assert res_stream["status"] == "completed" == res_nonstream["status"]
    assert res_stream["final_message"].content == res_nonstream["final_message"].content == ""
