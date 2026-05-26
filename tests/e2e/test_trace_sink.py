"""e2e trace sink coverage focused on sink fan-out and tool-call integration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import AgentSpec, InMemorySession, Runner, ToolCall
from observability import JsonlTraceSink
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
