from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from core.agent_spec import AgentSpec
from core.contracts import Event, LLMToolCallContract, LLMToolCallContractMode
from core.message import Message
from evolution.models import TranscriptMessage, TranscriptWindow
from evolution.reviewer_runtime import (
    REVIEWER_TOOL_NAME,
    _RecordingEventSink,
    build_restricted_reviewer_registry,
    run_child_review,
)


async def test_recording_event_sink_keeps_only_tool_events() -> None:
    sink = _RecordingEventSink(
        allowed_kinds=frozenset({"tool.call.start", "tool.call.end"}),
        allowed_tool_name=REVIEWER_TOOL_NAME,
        thread_id="thread-1",
        parent_run_id="run-1",
    )

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
            kind="tool.call.start",
            run_id="child-run",
            turn=1,
            payload={"tool_name": "documents", "call_id": "bad-1"},
        )
    )
    await sink.emit(
        Event(
            kind="tool.call.end",
            run_id="child-run",
            turn=1,
            payload={"tool_name": "documents", "call_id": "bad-1", "ok": False},
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
    assert all(event.payload["tool_name"] == REVIEWER_TOOL_NAME for event in events)


@pytest.mark.unit
async def test_recording_event_sink_logs_contract_violation_without_sensitive_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = _RecordingEventSink(
        allowed_kinds=frozenset({"tool.call.start", "tool.call.end"}),
        allowed_tool_name=REVIEWER_TOOL_NAME,
        thread_id="thread-1",
        parent_run_id="run-parent-1",
    )

    with caplog.at_level(logging.ERROR, logger="evolution.trigger_diagnostics"):
        await sink.emit(
            Event(
                kind="llm.tool_call.contract_violation",
                run_id="run-child-1",
                turn=1,
                payload={
                    "attempt": 1,
                    "violation_kind": "undeclared_tool",
                    "tool_name": "documents",
                    "tool_call_id": "bad-1",
                    "tool_index": 3,
                    "allowed_tool_names": ["evolution_write"],
                    "action": "retry",
                    "arguments": {"secret": "must-not-log"},
                },
            )
        )

    assert "category=reviewer_tool_contract_violation" in caplog.text
    assert "thread=thread-1" in caplog.text
    assert "run=run-parent-1" in caplog.text
    assert "child_run=run-child-1" in caplog.text
    assert "tool=documents" in caplog.text
    assert "must-not-log" not in caplog.text


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


def test_restricted_reviewer_registry_keeps_only_private_write_tool() -> None:
    parent_tools = {
        REVIEWER_TOOL_NAME: SimpleNamespace(name=REVIEWER_TOOL_NAME),
        "request_evolution_review": SimpleNamespace(name="request_evolution_review"),
    }

    registry = build_restricted_reviewer_registry(parent_tools)

    assert registry.names() == [REVIEWER_TOOL_NAME]


@pytest.mark.unit
async def test_run_child_review_treats_timeout_after_successful_write_as_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeRuntime:
        def __init__(self, review_session) -> None:  # type: ignore[no-untyped-def]
            self._review_session = review_session
            self.max_tokens: int | None = None
            self.max_turns: int | None = None
            self.tool_call_contract: LLMToolCallContract | None = None

        async def run(  # type: ignore[no-untyped-def]
            self,
            _prompt: str,
            *,
            session_id: str,
            max_tokens: int,
            max_turns: int,
            llm_tool_call_contract: LLMToolCallContract,
        ) -> None:
            self.max_tokens = max_tokens
            self.max_turns = max_turns
            self.tool_call_contract = llm_tool_call_contract
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

    built_runtime: _FakeRuntime | None = None

    def _fake_build(_config, *, session_factory, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal built_runtime
        built_runtime = _FakeRuntime(session_factory("evo-review-thread-1-run-1"))
        return built_runtime

    monkeypatch.setattr(
        "runtime_assembly.session_engine.SessionEngine.build",
        _fake_build,
    )

    review_model = SimpleNamespace(
        name="demo-model",
        preset_id="demo-preset",
        default_reasoning_effort=None,
        max_tokens=131_072,
    )
    catalog_manager = SimpleNamespace(resolve_runtime=lambda *_args, **_kwargs: review_model)
    parent_runtime = SimpleNamespace(
        config=SimpleNamespace(
            model=SimpleNamespace(preset_id="demo-preset"),
            evolution=SimpleNamespace(
                learning=SimpleNamespace(
                    preset_id=None,
                    reasoning_effort=None,
                )
            ),
        ),
        agent_spec=AgentSpec(name="parent", instructions="test", default_model="demo-model"),
        model_catalog_manager=catalog_manager,
        model_config=review_model,
        tools={REVIEWER_TOOL_NAME: SimpleNamespace(name=REVIEWER_TOOL_NAME)},
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
    assert built_runtime is not None
    assert built_runtime.max_tokens == 131_072
    assert built_runtime.max_turns == 1
    assert built_runtime.tool_call_contract is not None
    assert built_runtime.tool_call_contract.mode is LLMToolCallContractMode.DECLARED_EXACTLY_ONCE
    assert built_runtime.tool_call_contract.correction_retries == 1
