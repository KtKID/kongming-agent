"""e2e：provider_metadata 落盘链路。

验证 LLMResponse.provider_metadata 经 Runner emit llm.response 事件，
最终正确序列化到 JsonlTraceSink 的 JSONL 文件中。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import AgentSpec, InMemorySession, Runner
from core.contracts import LLMRequest, LLMResponse
from core.message import Message
from observability import JsonlTraceSink
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
    pm = payload["provider_metadata"]

    assert pm["cache_read_input_tokens"] == 50
    assert pm["id"] == "msg_test_123"
    assert pm["model"] == "claude-test"
