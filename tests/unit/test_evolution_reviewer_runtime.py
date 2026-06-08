from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from core.agent_spec import AgentSpec
from core.contracts import Event
from core.message import Message
from evolution.models import TranscriptMessage, TranscriptWindow
from evolution.reviewer_runtime import REVIEWER_TOOL_NAME, _RecordingEventSink, run_child_review


async def test_recording_event_sink_keeps_only_tool_events() -> None:
    sink = _RecordingEventSink(allowed_kinds=frozenset({"tool.call.start", "tool.call.end"}))

    await sink.emit(Event(kind="content.delta", run_id="child-run", turn=1))
    await sink.emit(
        Event(
            kind="tool.call.start",
            run_id="child-run",
            turn=1,
            payload={"tool_name": "evolution_write", "call_id": "c1"},
        )
    )
    await sink.emit(
        Event(
            kind="reasoning.delta",
            run_id="child-run",
            turn=1,
            payload={"delta": "hidden"},
        )
    )
    await sink.emit(
        Event(
            kind="tool.call.end",
            run_id="child-run",
            turn=1,
            payload={"tool_name": "evolution_write", "call_id": "c1", "ok": True},
        )
    )

    events = sink.snapshot()
    assert [event.kind for event in events] == [
        "tool.call.start",
        "tool.call.end",
    ]


def test_transcript_tool_message_is_rendered_as_plain_assistant_context() -> None:
    message = TranscriptMessage(
        turn=3,
        role="tool",
        content="write_file: ok",
        tool_name="write_file",
    ).to_message(0)

    assert message.role == "assistant"
    assert message.tool_call_id is None
    assert message.tool_calls is None
    assert message.content == "[turn 3][tool:write_file] write_file: ok"


def test_transcript_user_message_keeps_role_and_labels_turn() -> None:
    message = TranscriptMessage(
        turn=2,
        role="user",
        content="please summarize",
    ).to_message(0)

    assert message.role == "user"
    assert message.content == "[turn 2][user] please summarize"


@pytest.mark.unit
async def test_run_child_review_treats_timeout_after_successful_write_as_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeRuntime:
        def __init__(self, review_session) -> None:  # type: ignore[no-untyped-def]
            self._review_session = review_session

        async def run(self, _prompt: str, *, session_id: str):  # type: ignore[no-untyped-def]
            await self._review_session.append(
                Message.tool_result(
                    "call-review-1",
                    "stored",
                    name=REVIEWER_TOOL_NAME,
                    metadata={"ok": True, "data": {"status": "written"}},
                )
            )
            await asyncio.sleep(0.05)

        async def aclose(self) -> None:
            return None

    def _fake_build(_config, *, session_factory, **_kwargs):  # type: ignore[no-untyped-def]
        return _FakeRuntime(session_factory("evo-review-thread-1-run-1"))

    monkeypatch.setattr(
        "runtime_assembly.native_runtime.NativeRuntime.build",
        _fake_build,
    )

    parent_runtime = SimpleNamespace(
        config=SimpleNamespace(
            evolution=SimpleNamespace(
                learning=SimpleNamespace(model_name=None, reasoning_effort=None)
            )
        ),
        agent_spec=AgentSpec(name="parent", instructions="test", default_model="demo-model"),
        tools={REVIEWER_TOOL_NAME: object()},
    )
    window = TranscriptWindow(
        session_id="thread-1",
        run_id="run-1",
        user_turn_count=1,
        included_turns=(1,),
        messages=(TranscriptMessage(turn=1, role="user", content="remember this"),),
        final_message="ok",
        tool_call_count=0,
        summary="one turn",
    )

    outcome = await run_child_review(
        parent_runtime=parent_runtime,
        window=window,
        trigger_reason="cadence",
        timeout_seconds=0.01,
        max_nutrients=1,
        min_confidence=0.75,
    )

    assert outcome.write_ok is True
    assert outcome.write_status == "written"
    assert outcome.timed_out is True
    assert outcome.timeout_seconds == 0.01
    assert outcome.duration_ms >= 0
    assert outcome.result.metadata["timed_out_after_write"] is True
