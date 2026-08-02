"""unit：SessionEngine 的 self-evolution hook。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from core.contracts import Event, LLMRequest, LLMResponse, PreparedToolCall, ToolContext, ToolResult
from core.errors import MaxTurnsExceededError
from core.message import Message
from core.result import Result
from evolution.evolution_manager import EvolutionManager
from evolution.lifecycle import register_evolution_lifecycle_hook
from infrastructure.config.models import (
    ApprovalConfig,
    Config,
    EvolutionConfig,
    EvolutionLearningConfig,
    EvolutionMemoryConfig,
    FileToolConfig,
    ModelSelectionConfig,
    RunnerConfig,
    ShellToolConfig,
    ToolConfig,
)
from runtime_assembly.session_engine import SessionEngine
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

    async def execute(self, prepared: PreparedToolCall, ctx: ToolContext) -> ToolResult:
        del prepared, ctx
        return ToolResult(ok=True, content="ok")


class _Sink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        self.events.append(event)


def _cfg(
    tmp_path: Path,
    *,
    drain_on_close_seconds: float = 0.2,
    every_n_runs: int = 1,
    min_user_turns: int = 1,
) -> Config:
    return Config(
        model=ModelSelectionConfig(preset_id="local-gemma-4-e4b-it"),
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
                every_n_runs=every_n_runs,
                min_user_turns=min_user_turns,
                max_history_messages=10,
                max_nutrients=1,
                nutrient_confidence_threshold=0.75,
                review_timeout_seconds=1.0,
                drain_on_close_seconds=drain_on_close_seconds,
                root_path=str(tmp_path / ".kongming" / "evolution"),
            ),
        ),
    )


def _build_runtime_with_manager(
    tmp_path: Path,
    *,
    sink: _Sink | None = None,
    drain_on_close_seconds: float = 0.2,
) -> tuple[EvolutionManager, SessionEngine]:
    cfg = _cfg(tmp_path, drain_on_close_seconds=drain_on_close_seconds)
    manager = EvolutionManager(config=cfg, kongming_home=tmp_path)
    runtime = SessionEngine.build(
        cfg,
        tools=ToolRegistry([_FakeEvolutionWriteTool()]),
        enabled_tool_names=[],
        event_sinks=[sink] if sink is not None else [],
    )
    register_evolution_lifecycle_hook(runtime=runtime, manager=manager)
    runtime._llm = _StubLLM()  # type: ignore[attr-defined]
    return manager, runtime


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

    monkeypatch.setattr("evolution.reviewer_runtime.run_child_review", _fake_child_review)

    manager, runtime = _build_runtime_with_manager(tmp_path)

    result = await runtime.run("hello", session_id="s1")
    assert result.status == "completed"

    await manager.aclose()
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

    monkeypatch.setattr("evolution.reviewer_runtime.run_child_review", _slow_child_review)

    manager, runtime = _build_runtime_with_manager(
        tmp_path,
        sink=sink,
        drain_on_close_seconds=0.01,
    )

    await runtime.run("hello", session_id="s1")
    await manager.aclose()
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

    monkeypatch.setattr("evolution.reviewer_runtime.run_child_review", _failed_child_review)

    manager, runtime = _build_runtime_with_manager(tmp_path, sink=sink)

    await runtime.run("hello", session_id="s1")
    await manager.aclose()
    await runtime.aclose()

    assert any(event.kind == "evolution.review.failed" for event in sink.events)
    assert not any(event.kind == "evolution.review.completed" for event in sink.events)


@pytest.mark.unit
async def test_native_runtime_does_not_replay_child_tool_events_to_parent_sinks(
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

    monkeypatch.setattr(
        "evolution.reviewer_runtime.run_child_review", _child_review_with_tool_events
    )

    manager, runtime = _build_runtime_with_manager(tmp_path, sink=sink)

    await runtime.run("hello", session_id="s1")
    await manager.aclose()
    await runtime.aclose()

    # reviewer 的 tool.call.start/end 不应转发到父 thread 的 sinks（phase 踩踏修复）：
    # 这些事件的 run_id 属于 reviewer 子 agent，转发会覆盖主 run 的 complete 终态。
    tool_events = [event for event in sink.events if event.kind.startswith("tool.call.")]
    assert tool_events == []
    # evolution.review.completed 仍应到达父 thread 的 sinks（前端弹窗依赖）
    review_events = [event for event in sink.events if event.kind == "evolution.review.completed"]
    assert len(review_events) == 1


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

    monkeypatch.setattr("evolution.reviewer_runtime.run_child_review", _failed_child_review)

    manager, runtime = _build_runtime_with_manager(tmp_path, sink=sink)

    await runtime.run("hello", session_id="s1")
    await manager.aclose()
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

    monkeypatch.setattr("evolution.reviewer_runtime.run_child_review", _timeout_after_write)

    manager, runtime = _build_runtime_with_manager(tmp_path, sink=sink)

    await runtime.run("hello", session_id="s1")
    await manager.aclose()
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

    monkeypatch.setattr("evolution.reviewer_runtime.run_child_review", _timed_child_review)

    manager, runtime = _build_runtime_with_manager(tmp_path, sink=sink)

    await runtime.run("hello", session_id="s1")
    await manager.aclose()
    await runtime.aclose()

    started = next(event for event in sink.events if event.kind == "evolution.review.started")
    completed = next(event for event in sink.events if event.kind == "evolution.review.completed")
    assert started.payload["timeout_seconds"] == 1.0
    assert completed.payload["duration_ms"] == 3456
    assert completed.payload["timeout_hit"] is False
    assert completed.payload["timeout_seconds"] == 12.5


@pytest.mark.unit
async def test_runtime_channel_cadence_uses_session_manifest_single_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """runtime channel cadence 读 session manifest run_count（单一真源），不依赖 state.json。

    改造前 claude/runtime 两通道共享 EvolutionStateStore.run_count；改造后 runtime
    通道直接读 session manifest，与 claude 通道（仍用 state 自维护计数器）独立。

    本测试构造 every_n=2，连续两次 runtime.run()：第 1 次 manifest=1（不命中），
    第 2 次 manifest=2（2%2==0 命中）。验证 runtime cadence 完全由 session manifest 驱动。
    """
    calls: list[str] = []

    async def _fake_child_review(**kwargs: Any):  # type: ignore[no-untyped-def]
        from evolution.reviewer_runtime import ChildReviewOutcome

        window = kwargs["window"]
        calls.append(window.run_id)
        return ChildReviewOutcome(
            result=Result(
                run_id="review-run-shared",
                session_id="review-session",
                status="completed",
                final_message=Message.assistant("stored"),
                turn_count=1,
            ),
            write_ok=True,
            write_status="written",
            write_error=None,
        )

    monkeypatch.setattr("evolution.reviewer_runtime.run_child_review", _fake_child_review)

    cfg = _cfg(tmp_path, every_n_runs=2, min_user_turns=1)
    manager = EvolutionManager(config=cfg, kongming_home=tmp_path)
    thread_id = "thread-aabbccddeeff"

    runtime = SessionEngine.build(
        cfg,
        tools=ToolRegistry([_FakeEvolutionWriteTool()]),
        enabled_tool_names=[],
    )
    register_evolution_lifecycle_hook(runtime=runtime, manager=manager)
    runtime._llm = _StubLLM()  # type: ignore[attr-defined]

    # 第 1 次 run：advance_run_index 后 manifest run_count=1，1%2!=0 不命中
    await runtime.run("hello-1", session_id=thread_id)
    await asyncio.sleep(0.05)
    assert calls == []

    # 第 2 次 run：manifest run_count=2，2%2==0 命中
    result2 = await runtime.run("hello-2", session_id=thread_id)
    await asyncio.sleep(0.05)
    assert calls == [result2.run_id]

    await manager.aclose()
    await runtime.aclose()

    # state.json 的 run_count 不被 runtime channel 递增（保持 0）——单一真源在 manifest
    state_path = tmp_path / ".kongming" / "evolution" / "evolution.state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["sessions"][thread_id]["run_count"] == 0
