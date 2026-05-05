"""unit：NativeRuntime 的 self-evolution hook。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import evolution
from config_loader.models import (
    ApprovalConfig,
    Config,
    EvolutionConfig,
    EvolutionLearningConfig,
    EvolutionMemoryConfig,
    FileToolConfig,
    ModelConfig,
    RunnerConfig,
    ShellToolConfig,
    ToolConfig,
)
from core.contracts import Event, LLMRequest, LLMResponse, ToolContext, ToolResult
from core.errors import MaxTurnsExceededError
from core.message import Message
from core.result import Result
from executors.agent_runtime.native_runtime import NativeRuntime
from tools import ToolRegistry


class _StubLLM:
    async def complete(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            message=Message.assistant("ok"),
            finish_reason="stop",
        )


class _FakeEvolutionWriteTool:
    name = "evolution_write"
    description = "fake"
    input_schema: dict[str, Any] = {"type": "object"}

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(ok=True, content="ok")


class _Sink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


def _cfg(tmp_path: Path, *, drain_on_close_seconds: float = 0.2) -> Config:
    return Config(
        model=ModelConfig(
            provider="openai_compatible",
            name="gemma-4-e4b-it",
            base_url="http://127.0.0.1:1234/v1",
            api_key="",
        ),
        runner=RunnerConfig(max_turns=3),
        approval=ApprovalConfig(mode="auto_allow"),
        tool=ToolConfig(
            file=FileToolConfig(enabled=False),
            shell=ShellToolConfig(enabled=False),
        ),
        evolution=EvolutionConfig(
            memory=EvolutionMemoryConfig(enabled=False),
            learning=EvolutionLearningConfig(
                enabled=True,
                every_n_runs=1,
                min_user_turns=1,
                max_history_messages=10,
                max_nutrients=1,
                nutrient_confidence_threshold=0.75,
                review_timeout_seconds=1.0,
                drain_on_close_seconds=drain_on_close_seconds,
                root_path=str(tmp_path / ".kongming" / "evolution"),
            ),
        ),
    )


@pytest.mark.unit
async def test_native_runtime_schedules_child_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[str, str]] = []

    async def _fake_child_review(**kwargs: Any):  # type: ignore[no-untyped-def]
        from evolution.reviewer_runtime import ChildReviewOutcome

        window = kwargs["window"]
        calls.append((kwargs["trigger_reason"], window.run_id))
        return ChildReviewOutcome(
            result=Result(
                run_id="review-run-1",
                session_id="review-session",
                status="completed",
                final_message=Message.assistant("stored"),
                turn_count=1,
            ),
            write_ok=True,
            write_status="written",
            write_error=None,
            duration_ms=120,
            timeout_seconds=1.0,
        )

    monkeypatch.setattr(evolution, "run_child_review", _fake_child_review)

    runtime = NativeRuntime.build(
        _cfg(tmp_path),
        tools=ToolRegistry([_FakeEvolutionWriteTool()]),
        enabled_tool_names=[],
    )
    runtime._llm = _StubLLM()  # type: ignore[attr-defined]

    result = await runtime.run("hello", session_id="s1")
    assert result.status == "completed"

    await runtime.aclose()
    assert calls == [("cadence", result.run_id)]


@pytest.mark.unit
async def test_native_runtime_aclose_emits_drain_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()
    sink = _Sink()

    async def _slow_child_review(**kwargs: Any):  # type: ignore[no-untyped-def]
        from evolution.reviewer_runtime import ChildReviewOutcome

        await gate.wait()
        return ChildReviewOutcome(
            result=Result(
                run_id="review-run-2",
                session_id="review-session",
                status="completed",
                final_message=Message.assistant("stored"),
                turn_count=1,
            ),
            write_ok=True,
            write_status="written",
            write_error=None,
        )

    monkeypatch.setattr(evolution, "run_child_review", _slow_child_review)

    runtime = NativeRuntime.build(
        _cfg(tmp_path, drain_on_close_seconds=0.01),
        tools=ToolRegistry([_FakeEvolutionWriteTool()]),
        enabled_tool_names=[],
        event_sinks=[sink],
    )
    runtime._llm = _StubLLM()  # type: ignore[attr-defined]

    await runtime.run("hello", session_id="s1")
    await runtime.aclose()

    assert any(event.kind == "evolution.review.drain_timeout" for event in sink.events)


@pytest.mark.unit
async def test_native_runtime_marks_review_failed_when_write_did_not_succeed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sink = _Sink()

    async def _failed_child_review(**kwargs: Any):  # type: ignore[no-untyped-def]
        from evolution.reviewer_runtime import ChildReviewOutcome

        return ChildReviewOutcome(
            result=Result(
                run_id="review-run-3",
                session_id="review-session",
                status="completed",
                final_message=Message.assistant("stored"),
                turn_count=2,
            ),
            write_ok=False,
            write_status="error",
            write_error="run_id must be a non-empty string",
        )

    monkeypatch.setattr(evolution, "run_child_review", _failed_child_review)

    runtime = NativeRuntime.build(
        _cfg(tmp_path),
        tools=ToolRegistry([_FakeEvolutionWriteTool()]),
        enabled_tool_names=[],
        event_sinks=[sink],
    )
    runtime._llm = _StubLLM()  # type: ignore[attr-defined]

    await runtime.run("hello", session_id="s1")
    await runtime.aclose()

    assert any(event.kind == "evolution.review.failed" for event in sink.events)
    assert not any(event.kind == "evolution.review.completed" for event in sink.events)


@pytest.mark.unit
async def test_native_runtime_replays_child_tool_events_to_parent_sinks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sink = _Sink()

    async def _child_review_with_tool_events(**kwargs: Any):  # type: ignore[no-untyped-def]
        from evolution.reviewer_runtime import ChildReviewOutcome

        return ChildReviewOutcome(
            result=Result(
                run_id="review-run-4",
                session_id="review-session",
                status="completed",
                final_message=Message.assistant("stored"),
                turn_count=1,
            ),
            write_ok=True,
            write_status="written",
            write_error=None,
            visible_events=(
                Event(
                    kind="tool.call.start",
                    run_id="review-run-4",
                    turn=1,
                    payload={"tool_name": "evolution_write", "call_id": "c-review-1"},
                ),
                Event(
                    kind="tool.call.end",
                    run_id="review-run-4",
                    turn=1,
                    payload={
                        "tool_name": "evolution_write",
                        "call_id": "c-review-1",
                        "ok": True,
                        "content": "stored",
                    },
                ),
            ),
        )

    monkeypatch.setattr(evolution, "run_child_review", _child_review_with_tool_events)

    runtime = NativeRuntime.build(
        _cfg(tmp_path),
        tools=ToolRegistry([_FakeEvolutionWriteTool()]),
        enabled_tool_names=[],
        event_sinks=[sink],
    )
    runtime._llm = _StubLLM()  # type: ignore[attr-defined]

    await runtime.run("hello", session_id="s1")
    await runtime.aclose()

    tool_events = [event for event in sink.events if event.kind.startswith("tool.call.")]
    assert [event.kind for event in tool_events] == [
        "tool.call.start",
        "tool.call.end",
    ]
    assert tool_events[0].run_id == "review-run-4"
    assert tool_events[1].payload["content"] == "stored"


@pytest.mark.unit
async def test_native_runtime_surfaces_child_reviewer_error_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sink = _Sink()

    async def _failed_child_review(**kwargs: Any):  # type: ignore[no-untyped-def]
        from evolution.reviewer_runtime import ChildReviewOutcome

        return ChildReviewOutcome(
            result=Result(
                run_id="review-run-5",
                session_id="review-session",
                status="failed",
                final_message=None,
                turn_count=2,
                error=MaxTurnsExceededError(
                    "exceeded max_turns=2 without reaching a terminal response"
                ),
            ),
            write_ok=False,
            write_status=None,
            write_error=None,
        )

    monkeypatch.setattr(evolution, "run_child_review", _failed_child_review)

    runtime = NativeRuntime.build(
        _cfg(tmp_path),
        tools=ToolRegistry([_FakeEvolutionWriteTool()]),
        enabled_tool_names=[],
        event_sinks=[sink],
    )
    runtime._llm = _StubLLM()  # type: ignore[attr-defined]

    await runtime.run("hello", session_id="s1")
    await runtime.aclose()

    failed = next(event for event in sink.events if event.kind == "evolution.review.failed")
    assert failed.payload["error_kind"] == "MaxTurnsExceededError"
    assert "exceeded max_turns=2" in str(failed.payload["message"])
    assert failed.payload["child_status"] == "failed"


@pytest.mark.unit
async def test_native_runtime_keeps_written_status_when_child_times_out_after_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sink = _Sink()

    async def _timeout_after_write(**kwargs: Any):  # type: ignore[no-untyped-def]
        from evolution.reviewer_runtime import ChildReviewOutcome

        return ChildReviewOutcome(
            result=Result(
                run_id="review-run-6",
                session_id="review-session",
                status="failed",
                final_message=None,
                turn_count=2,
            ),
            write_ok=True,
            write_status="written",
            write_error=None,
            timed_out=True,
        )

    monkeypatch.setattr(evolution, "run_child_review", _timeout_after_write)

    runtime = NativeRuntime.build(
        _cfg(tmp_path),
        tools=ToolRegistry([_FakeEvolutionWriteTool()]),
        enabled_tool_names=[],
        event_sinks=[sink],
    )
    runtime._llm = _StubLLM()  # type: ignore[attr-defined]

    await runtime.run("hello", session_id="s1")
    await runtime.aclose()

    completed = next(event for event in sink.events if event.kind == "evolution.review.completed")
    assert completed.payload["write_status"] == "written"
    assert completed.payload["timed_out_after_write"] is True
    assert completed.payload["duration_ms"] == 0
    assert completed.payload["timeout_hit"] is True
    assert not any(event.kind == "evolution.review.failed" for event in sink.events)


@pytest.mark.unit
async def test_native_runtime_emits_review_duration_and_timeout_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sink = _Sink()

    async def _timed_child_review(**kwargs: Any):  # type: ignore[no-untyped-def]
        from evolution.reviewer_runtime import ChildReviewOutcome

        return ChildReviewOutcome(
            result=Result(
                run_id="review-run-7",
                session_id="review-session",
                status="completed",
                final_message=Message.assistant("stored"),
                turn_count=1,
            ),
            write_ok=True,
            write_status="written",
            write_error=None,
            duration_ms=3456,
            timeout_seconds=12.5,
        )

    monkeypatch.setattr(evolution, "run_child_review", _timed_child_review)

    runtime = NativeRuntime.build(
        _cfg(tmp_path),
        tools=ToolRegistry([_FakeEvolutionWriteTool()]),
        enabled_tool_names=[],
        event_sinks=[sink],
    )
    runtime._llm = _StubLLM()  # type: ignore[attr-defined]

    await runtime.run("hello", session_id="s1")
    await runtime.aclose()

    started = next(event for event in sink.events if event.kind == "evolution.review.started")
    completed = next(event for event in sink.events if event.kind == "evolution.review.completed")
    assert started.payload["timeout_seconds"] == 1.0
    assert completed.payload["duration_ms"] == 3456
    assert completed.payload["timeout_hit"] is False
    assert completed.payload["timeout_seconds"] == 12.5
