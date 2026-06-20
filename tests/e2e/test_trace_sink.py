"""e2e trace sink coverage focused on sink fan-out and tool-call integration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import AgentSpec, InMemorySession, Runner, ToolCall
from infrastructure.tracing import JsonlTraceSink
from sessions.file_session import FileSession
from sessions.session_bootstrap import SessionBootstrap
from tests.e2e.conftest import MemoryEventSink, RecordingApproval, StubLLMProvider
from tools import ReadFileTool


def _read_jsonl_events(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


@pytest.mark.e2e
async def test_trace_sink_and_memory_sink_both_receive_same_events(
    stub_llm: StubLLMProvider,
    recording_approval: RecordingApproval,
    memory_sink: MemoryEventSink,
    tmp_path: Path,
) -> None:
    stub_llm.script(content="ok")
    trace_path = tmp_path / "multi.jsonl"
    sink = JsonlTraceSink(trace_path)

    runner = Runner(event_sinks=[sink, memory_sink])
    session = InMemorySession("fanout")
    spec = AgentSpec(
        name="t",
        instructions="",
        default_model="stub",
        tool_names=(),
        max_turns=3,
    )

    result = await runner.run(
        "hi",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )
    assert result.status == "completed"

    mem_kinds = memory_sink.kinds()
    trace_events = _read_jsonl_events(trace_path)
    trace_kinds = [e["kind"] for e in trace_events]
    persisted_mem_kinds = [k for k in mem_kinds if k not in {"content.delta", "reasoning.delta"}]

    assert persisted_mem_kinds == trace_kinds
    assert all(e["run_id"] == result.run_id for e in trace_events)
    assert all(e.run_id == result.run_id for e in memory_sink.events)


@pytest.mark.e2e
async def test_trace_sink_captures_tool_call_events(
    stub_llm: StubLLMProvider,
    recording_approval: RecordingApproval,
    tmp_path: Path,
) -> None:
    target = tmp_path / "file.txt"
    target.write_text("hello")

    stub_llm.script(
        tool_calls=[
            ToolCall(
                call_id="t1",
                tool_name="read_file",
                arguments={"path": str(target)},
            )
        ],
    )
    stub_llm.script(content="read complete")

    trace_path = tmp_path / "tool-trace.jsonl"
    sink = JsonlTraceSink(trace_path)
    runner = Runner(event_sinks=[sink])
    session = InMemorySession("tool-trace")
    spec = AgentSpec(
        name="t",
        instructions="",
        default_model="stub",
        tool_names=("read_file",),
        max_turns=5,
    )

    result = await runner.run(
        "read please",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools={"read_file": ReadFileTool()},
        approval=recording_approval,
    )
    assert result.status == "completed"

    events = _read_jsonl_events(trace_path)
    kinds = [e["kind"] for e in events]

    assert "tool.call.start" in kinds
    assert "tool.call.end" in kinds
    assert "approval.request" in kinds
    assert "approval.decision" in kinds

    tool_end = [e for e in events if e["kind"] == "tool.call.end"]
    assert len(tool_end) == 1
    assert tool_end[0]["payload"]["ok"] is True
    assert tool_end[0]["payload"]["call_id"] == "t1"
    assert tool_end[0]["payload"]["tool_name"] == "read_file"


@pytest.mark.e2e
async def test_trace_records_llm_request_summary(
    stub_llm: StubLLMProvider,
    recording_approval: RecordingApproval,
    tmp_path: Path,
) -> None:
    target = tmp_path / "audit.txt"
    target.write_text("audit payload", encoding="utf-8")

    stub_llm.script(
        tool_calls=[
            ToolCall(
                call_id="audit-call",
                tool_name="read_file",
                arguments={"path": str(target)},
            )
        ],
    )
    stub_llm.script(content="done")

    trace_path = tmp_path / "audit-trace.jsonl"
    session = FileSession(
        "audit-session",
        SessionBootstrap(
            agent_name="audit-agent",
            model_name="stub-model",
            instruction_sources=[],
            instruction_text_hash="sha256:audit",
            created_at=1.0,
            cwd=str(tmp_path),
            app_version="test",
        ),
        str(tmp_path / "sessions"),
    )
    runner = Runner(event_sinks=[JsonlTraceSink(trace_path)])
    spec = AgentSpec(
        name="audit-agent",
        instructions="Follow audit rules.",
        default_model="stub-model",
        tool_names=("read_file",),
        max_turns=5,
        reasoning_effort="high",
    )

    result = await runner.run(
        "read audit file",
        session=session,
        agent_spec=spec,
        llm=stub_llm,
        tools={"read_file": ReadFileTool()},
        approval=recording_approval,
    )

    assert result.status == "completed"

    session_path = tmp_path / "sessions" / "audit-session" / "audit-session.jsonl"
    records = _read_jsonl_events(session_path)
    message_rows = [row for row in records if row.get("record_type", "message") == "message"]

    assert any(row["message"]["role"] == "assistant" for row in message_rows)
    assert any(
        row["message"]["role"] == "tool" and row["message"]["content"] == "audit payload"
        for row in message_rows
    )
    assert [message.role for message in await session.history()] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]

    trace_events = _read_jsonl_events(trace_path)
    trace_request = next(row for row in trace_events if row["kind"] == "llm.request")
    trace_response = next(row for row in trace_events if row["kind"] == "llm.response")
    first_trace_request = trace_request["payload"]["request"]
    assert first_trace_request["model"] == "stub-model"
    assert first_trace_request["reasoning_effort"] == "high"
    assert first_trace_request["message_roles"] == ["system", "user"]
    assert first_trace_request["tool_names"] == ["read_file"]
    assert "messages" not in first_trace_request
    assert "tools" not in first_trace_request
    first_trace_response = trace_response["payload"]["response"]
    assert first_trace_response["finish_reason"] == "tool_calls"
    assert first_trace_response["message"]["tool_call_count"] == 1
    assert first_trace_response["message"]["tool_names"] == ["read_file"]
