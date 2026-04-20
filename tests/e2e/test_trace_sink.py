"""e2e P1-2：trace 落盘链路。

覆盖 plan.md "e2e 主链路基线"的第五条：

> 一次 run 结束后，事件经 ``list[EventSink]`` fan-out → ``trace_sink.py`` JSONL 落盘。

验证 :class:`observability.JsonlTraceSink` 注入 runner 后：

- 一次 run 结束会在磁盘上产生一份可读的 JSONL
- 每行都是合法 JSON，至少覆盖 ``run.start`` / ``turn.start`` / ``llm.request`` /
  ``llm.response`` / ``run.end`` 几种 kind
- 多 sink 同时注入时，fan-out 给每个 sink 的事件序列一致
- 第二次 run 在同一个 trace 文件里**追加**而不是覆盖
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import AgentSpec, InMemorySession, Runner, ToolCall
from observability import JsonlTraceSink
from tests.e2e.conftest import MemoryEventSink, RecordingApproval, StubLLMProvider
from tools import ReadFileTool


def _read_jsonl_events(path: Path) -> list[dict]:
    """把 trace 文件读回来、每行 JSON 反序列化成 dict。"""
    raw = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


@pytest.mark.e2e
async def test_trace_jsonl_written_and_parseable(
    stub_llm: StubLLMProvider,
    recording_approval: RecordingApproval,
    tmp_path: Path,
) -> None:
    """最小 run 结束后 JSONL 落盘，每行是合法 JSON，覆盖关键 kind。"""
    stub_llm.script(content="done")
    trace_path = tmp_path / "trace.jsonl"
    sink = JsonlTraceSink(trace_path)

    runner = Runner(event_sinks=[sink])
    session = InMemorySession("trace-1")
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

    # 文件存在且非空
    assert trace_path.exists()
    assert trace_path.stat().st_size > 0, "trace file is empty"

    # 每行都是合法 JSON
    events = _read_jsonl_events(trace_path)
    assert len(events) > 0

    # 每条事件至少有 kind + run_id 两个字段
    for ev in events:
        assert "kind" in ev
        assert "run_id" in ev
        assert ev["run_id"] == result.run_id

    kinds = [e["kind"] for e in events]
    # v1-mini 主链路应至少覆盖这些 kind
    assert "run.start" in kinds
    assert "run.end" in kinds
    assert "turn.start" in kinds
    assert "turn.end" in kinds
    # llm 事件至少一个（llm.request / llm.response）
    assert any(k.startswith("llm.") for k in kinds), f"no llm.* events found, got: {kinds}"
    # run.start / run.end 各恰好一次
    assert kinds.count("run.start") == 1
    assert kinds.count("run.end") == 1


@pytest.mark.e2e
async def test_trace_sink_and_memory_sink_both_receive_same_events(
    stub_llm: StubLLMProvider,
    recording_approval: RecordingApproval,
    memory_sink: MemoryEventSink,
    tmp_path: Path,
) -> None:
    """Runner fan-out：多个 sink 看到相同顺序、相同数量的事件。"""
    stub_llm.script(content="ok")
    trace_path = tmp_path / "multi.jsonl"
    sink = JsonlTraceSink(trace_path)

    # 两个 sink 同时注册到 runner
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

    # 事件数一致
    assert len(mem_kinds) == len(trace_kinds)
    # 顺序也一致（单协程 fan-out，按注册顺序逐个 await）
    assert mem_kinds == trace_kinds

    # 两侧的 run_id 都和 Result.run_id 一致
    assert all(e["run_id"] == result.run_id for e in trace_events)
    assert all(e.run_id == result.run_id for e in memory_sink.events)


@pytest.mark.e2e
async def test_trace_sink_appends_across_runs(
    stub_llm: StubLLMProvider,
    recording_approval: RecordingApproval,
    tmp_path: Path,
) -> None:
    """同一 sink 连跑两次 run，第二次应在原文件基础上追加，而不是覆盖。"""
    trace_path = tmp_path / "append.jsonl"
    sink = JsonlTraceSink(trace_path)
    runner = Runner(event_sinks=[sink])
    spec = AgentSpec(
        name="t",
        instructions="",
        default_model="stub",
        tool_names=(),
        max_turns=3,
    )

    # 第一次 run
    stub_llm.script(content="first")
    first_result = await runner.run(
        "1",
        session=InMemorySession("r1"),
        agent_spec=spec,
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )
    assert first_result.status == "completed"
    first_events = _read_jsonl_events(trace_path)
    first_count = len(first_events)
    assert first_count > 0

    # 第二次 run
    stub_llm.script(content="second")
    second_result = await runner.run(
        "2",
        session=InMemorySession("r2"),
        agent_spec=spec,
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )
    assert second_result.status == "completed"
    all_events = _read_jsonl_events(trace_path)
    second_count = len(all_events)

    # 事件数严格增加
    assert second_count > first_count
    # 前 first_count 条仍然属于第一次 run
    for ev in all_events[:first_count]:
        assert ev["run_id"] == first_result.run_id
    # 后续新增的事件至少有一些属于第二次 run
    new_run_ids = {ev["run_id"] for ev in all_events[first_count:]}
    assert second_result.run_id in new_run_ids
    # 两次 run 的 run_id 不同（runner 自生成）
    assert first_result.run_id != second_result.run_id


@pytest.mark.e2e
async def test_trace_sink_captures_tool_call_events(
    stub_llm: StubLLMProvider,
    recording_approval: RecordingApproval,
    tmp_path: Path,
) -> None:
    """触发一次 tool_call，trace 文件应包含 tool.call.* 和 approval.* 事件。"""
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

    # tool 相关事件齐全
    assert "tool.call.start" in kinds
    assert "tool.call.end" in kinds
    assert "approval.request" in kinds
    assert "approval.decision" in kinds

    # tool.call.end 的 payload.ok == True（approval approved + tool 成功）
    tool_end = [e for e in events if e["kind"] == "tool.call.end"]
    assert len(tool_end) == 1
    assert tool_end[0]["payload"]["ok"] is True
    assert tool_end[0]["payload"]["call_id"] == "t1"
    assert tool_end[0]["payload"]["tool_name"] == "read_file"


@pytest.mark.e2e
async def test_trace_sink_serializes_event_payload_fields(
    stub_llm: StubLLMProvider,
    recording_approval: RecordingApproval,
    tmp_path: Path,
) -> None:
    """trace 每行是真正的 Event dataclass 序列化，payload / timestamp 都有。"""
    stub_llm.script(content="hi")
    trace_path = tmp_path / "shape.jsonl"
    sink = JsonlTraceSink(trace_path)
    runner = Runner(event_sinks=[sink])

    result = await runner.run(
        "ping",
        session=InMemorySession("shape"),
        agent_spec=AgentSpec(
            name="t",
            instructions="",
            default_model="stub",
            tool_names=(),
            max_turns=3,
        ),
        llm=stub_llm,
        tools={},
        approval=recording_approval,
    )
    assert result.status == "completed"

    events = _read_jsonl_events(trace_path)
    assert events, "expected at least one event"

    # 每条事件都有 Event dataclass 的全部字段（asdict 之后的 dict 形态）
    for ev in events:
        assert "kind" in ev
        assert "run_id" in ev
        assert "turn" in ev  # 可能是 None，但 key 必须存在
        assert "payload" in ev
        assert isinstance(ev["payload"], dict)
        assert "timestamp_ms" in ev
        assert isinstance(ev["timestamp_ms"], int)

    # run.start payload 里会带 session_id / model / max_turns 等装配信息
    run_start_events = [e for e in events if e["kind"] == "run.start"]
    assert len(run_start_events) == 1
    start_payload = run_start_events[0]["payload"]
    assert start_payload["session_id"] == "shape"
    assert start_payload["model"] == "stub"
    assert start_payload["max_turns"] == 3

    # run.end payload 带 status / turn_count
    run_end_events = [e for e in events if e["kind"] == "run.end"]
    assert len(run_end_events) == 1
    end_payload = run_end_events[0]["payload"]
    assert end_payload["status"] == "completed"
    assert end_payload["turn_count"] == result.turn_count
