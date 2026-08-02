"""e2e：provider_metadata 落盘链路。

验证 LLMResponse.provider_metadata 经 Runner emit llm.response 事件，
最终正确序列化到 JsonlTraceSink 的 JSONL 文件中。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import AgentSpec, InMemorySession, Runner
from core.contracts import (
    LLMRequest,
    LLMResponse,
    PreparedToolCall,
    ToolContext,
    ToolResult,
)
from core.message import Message
from infrastructure.tracing import JsonlTraceSink
from prompting.assembly.input_assembler import InputAssembler
from prompting.instructions.instruction_loader import InstructionSource
from tests.e2e.conftest import RecordingApproval

# ---------------------------------------------------------------------------
# Local stub with provider_metadata support
# ---------------------------------------------------------------------------


class StubLLMWithMetadata:
    """返回固定 provider_metadata 的最小 LLMProvider stub。"""

    def __init__(self, metadata: dict) -> None:
        self._metadata = metadata
        self._called = False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if not self._called:
            self._called = True
            return LLMResponse(
                message=Message(role="assistant", content="done"),
                finish_reason="stop",
                provider_metadata=self._metadata,
            )
        # 已调用一次后返回空响应终止 runner
        return LLMResponse(
            message=Message(role="assistant", content=""),
            finish_reason="stop",
        )


class EchoTool:
    """用于验证 llm.request 记录工具摘要的测试工具。"""

    name = "echo"
    description = "Echo the provided text."
    input_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def execute(
        self,
        prepared: PreparedToolCall,
        ctx: ToolContext,
    ) -> ToolResult:
        del ctx
        return ToolResult(ok=True, content=str(prepared.arguments.get("text", "")))


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _read_jsonl_events(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.e2e
async def test_provider_metadata_lands_in_trace_jsonl(
    recording_approval: RecordingApproval,
    tmp_path: Path,
) -> None:
    """provider_metadata 从 LLMResponse 经 Runner 落盘到 JSONL 的 llm.response 事件。"""
    metadata = {
        "cache_read_input_tokens": 50,
        "id": "msg_test_123",
        "model": "claude-test",
    }
    llm = StubLLMWithMetadata(metadata=metadata)

    trace_path = tmp_path / "metadata_trace.jsonl"
    sink = JsonlTraceSink(trace_path)
    runner = Runner(event_sinks=[sink])
    session = InMemorySession("meta-1")
    spec = AgentSpec(
        name="t",
        instructions="",
        default_model="stub",
        tool_names=(),
        max_turns=2,
    )

    result = await runner.run(
        "hello",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={},
        approval=recording_approval,
    )
    assert result.status == "completed"

    # 文件存在且非空
    assert trace_path.exists()
    assert trace_path.stat().st_size > 0

    events = _read_jsonl_events(trace_path)
    llm_response_events = [e for e in events if e["kind"] == "llm.response"]
    assert len(llm_response_events) >= 1, (
        f"no llm.response events found, got kinds: {[e['kind'] for e in events]}"
    )

    # 取第一个 llm.response 事件（带 metadata 的那次）
    first_resp = llm_response_events[0]
    assert "payload" in first_resp
    payload = first_resp["payload"]

    assert "provider_metadata" in payload, f"provider_metadata missing from payload: {payload}"
    assert payload["response"]["message"]["role"] == "assistant"
    assert payload["response"]["message"]["content_chars"] == 4
    pm = payload["provider_metadata"]

    assert pm["cache_read_input_tokens"] == 50
    assert pm["id"] == "msg_test_123"
    assert pm["model"] == "claude-test"


@pytest.mark.e2e
async def test_llm_request_lands_in_trace_jsonl_with_assembled_summary(
    recording_approval: RecordingApproval,
    tmp_path: Path,
) -> None:
    """llm.request 事件记录摘要，保留索引字段并剔除正文和 schema。"""

    llm = StubLLMWithMetadata(metadata={})
    trace_path = tmp_path / "request_trace.jsonl"
    sink = JsonlTraceSink(trace_path)
    runner = Runner(
        event_sinks=[sink],
        input_assembler=InputAssembler(),
        instruction_sources=[
            InstructionSource(
                origin="runtime",
                content="完整 assembly 后 system prompt",
            )
        ],
    )
    session = InMemorySession("request-1")
    spec = AgentSpec(
        name="t",
        instructions="",
        default_model="stub-model",
        tool_names=("echo",),
        max_turns=2,
    )

    result = await runner.run(
        "hello",
        session=session,
        agent_spec=spec,
        llm=llm,
        tools={"echo": EchoTool()},
        approval=recording_approval,
    )

    assert result.status == "completed"
    events = _read_jsonl_events(trace_path)
    llm_request_events = [e for e in events if e["kind"] == "llm.request"]
    assert llm_request_events, (
        f"no llm.request event found, got kinds: {[e['kind'] for e in events]}"
    )

    payload = llm_request_events[0]["payload"]
    request = payload["request"]

    assert request["model"] == "stub-model"
    assert request["metadata"] == {"thread_id": "request-1"}
    assert request["temperature"] is None
    assert request["max_tokens"] is None
    assert request["timeout_seconds"] is None

    assert request["message_roles"] == ["system", "user"]
    assert request["tool_names"] == ["echo"]
    assert "messages" not in request
    assert "tools" not in request

    assert payload["model"] == request["model"]
    assert payload["message_count"] == request["message_count"]
    assert payload["tool_count"] == request["tool_count"]
