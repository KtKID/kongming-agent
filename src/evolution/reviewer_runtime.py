"""Child reviewer runtime。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.agent_spec import AgentSpec
from core.contracts import Event, EventSink
from core.message import Message
from core.result import Result
from core.session import InMemorySession
from evolution.models import TranscriptWindow
from tools import AutoAllowApproval

if TYPE_CHECKING:
    from executors.agent_runtime.native_runtime import NativeRuntime

REVIEWER_TOOL_NAME = "evolution_write"
_REVIEWER_CAPTURED_EVENT_KINDS = frozenset(
    {
        "tool.call.start",
        "tool.call.end",
    }
)


@dataclass(frozen=True)
class ChildReviewOutcome:
    result: Result
    write_ok: bool
    write_status: str | None
    write_error: str | None
    write_data: dict[str, object] | None = None
    visible_events: tuple[Event, ...] = ()
    timed_out: bool = False
    duration_ms: int = 0
    timeout_seconds: float | None = None


class _RecordingEventSink(EventSink):
    """录制 child reviewer 的工具事件，供 parent run 回放到主聊天流。"""

    def __init__(self, *, allowed_kinds: frozenset[str]) -> None:
        self._allowed_kinds = allowed_kinds
        self._events: list[Event] = []

    async def emit(self, event: Event) -> None:
        if event.kind not in self._allowed_kinds:
            return
        self._events.append(event)

    def snapshot(self) -> tuple[Event, ...]:
        return tuple(self._events)


def _build_reviewer_instructions(*, max_nutrients: int, min_confidence: float) -> str:
    return (
        "你是 evolution-reviewer。请审阅上方 transcript，只提取高价值的进化养料。"
        "重点关注稳定偏好、可复用流程、可沉淀的错误恢复经验。"
        "只写提纯后的内容，不要抄整段聊天。"
        "evidence_turns 必须具体。"
        f"最多写 {max_nutrients} 条养料。"
        f"confidence 低于 {min_confidence:.2f} 的内容直接丢弃。"
        "必须且只能调用一次 evolution_write。"
    )


def _build_review_prompt(
    *,
    trigger_reason: str,
    window: TranscriptWindow,
    max_nutrients: int,
    min_confidence: float,
) -> str:
    return (
        "请复盘这个 session 里已经加载好的 transcript。\n"
        f"触发原因: {trigger_reason}\n"
        f"纳入复盘的 turns: {list(window.included_turns)}\n"
        f"窗口摘要: {window.summary or 'n/a'}\n"
        "请通过调用一次 evolution_write 返回结果，入参结构如下：\n"
        "{\n"
        '  "review_result": {\n'
        f'    "run_id": "{window.run_id}",\n'
        f'    "session_id": "{window.session_id}",\n'
        '    "reviewed_at_ms": <int>,\n'
        '    "review_summary": "<短摘要>",\n'
        '    "nutrients": [\n'
        "      {\n"
        '        "nutrient_id": "<稳定 id>",\n'
        '        "kind": "memory|workflow|error",\n'
        '        "title": "<短标题>",\n'
        '        "content": "<提纯后的内容>",\n'
        '        "summary": "<一行摘要>",\n'
        '        "confidence": <0-1>,\n'
        '        "evidence_turns": [<turn 整数列表>],\n'
        f'        "source_run_id": "{window.run_id}",\n'
        f'        "source_session_id": "{window.session_id}",\n'
        '        "suggested_target": "memory|skill|errorbook|null",\n'
        '        "tags": ["..."]\n'
        "      }\n"
        "    ],\n"
        '    "skip_reasons": ["..."]\n'
        "  },\n"
        '  "trigger_reason": "<同样的触发原因>",\n'
        '  "transcript_window": <当前已加载的窗口元数据>\n'
        "}\n"
        f"养料最多 {max_nutrients} 条，只保留 confidence >= {min_confidence:.2f} 的内容。"
    )


async def run_child_review(
    *,
    parent_runtime: NativeRuntime,
    window: TranscriptWindow,
    trigger_reason: str,
    timeout_seconds: float,
    max_nutrients: int,
    min_confidence: float,
) -> ChildReviewOutcome:
    from executors.agent_runtime.native_runtime import NativeRuntime

    learning = parent_runtime.config.evolution.learning
    review_session_id = f"evo-review-{window.session_id}-{window.run_id}"
    review_session = InMemorySession(session_id=review_session_id)
    for index, message in enumerate(window.messages):
        await review_session.append(message.to_message(index))

    reviewer_spec = AgentSpec(
        name="evolution-reviewer",
        instructions=_build_reviewer_instructions(
            max_nutrients=max_nutrients,
            min_confidence=min_confidence,
        ),
        default_model=learning.model_name or parent_runtime.agent_spec.default_model,
        tool_names=(REVIEWER_TOOL_NAME,),
        max_turns=2,
        metadata={"evolution_role": "reviewer"},
        reasoning_effort=learning.reasoning_effort or parent_runtime.agent_spec.reasoning_effort,
    )

    def _session_factory(sid: str):  # type: ignore[no-untyped-def]
        if sid == review_session_id:
            return review_session
        return InMemorySession(session_id=sid)

    recording_sink = _RecordingEventSink(allowed_kinds=_REVIEWER_CAPTURED_EVENT_KINDS)
    runtime = NativeRuntime.build(
        parent_runtime.config,
        event_sinks=[recording_sink],
        approval=AutoAllowApproval(),
        tools=parent_runtime.tools,
        enabled_tool_names=[REVIEWER_TOOL_NAME],
        session_factory=_session_factory,
        agent_spec=reviewer_spec,
    )
    started_at = time.perf_counter()
    try:
        try:
            result = await asyncio.wait_for(
                runtime.run(
                    _build_review_prompt(
                        trigger_reason=trigger_reason,
                        window=window,
                        max_nutrients=max_nutrients,
                        min_confidence=min_confidence,
                    ),
                    session_id=review_session_id,
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            history = await review_session.history()
            write_ok = _review_write_ok(history)
            write_status = _review_write_status(history)
            write_error = _review_write_error(history)
            duration_ms = _duration_ms(started_at)
            if not write_ok:
                raise
            visible_events = recording_sink.snapshot()
            return ChildReviewOutcome(
                result=Result(
                    run_id=_infer_review_run_id(visible_events, fallback=review_session_id),
                    session_id=review_session_id,
                    status="failed",
                    final_message=None,
                    turn_count=0,
                    metadata={"timed_out_after_write": True},
                ),
                write_ok=write_ok,
                write_status=write_status,
                write_error=write_error,
                write_data=_review_write_data(history),
                visible_events=visible_events,
                timed_out=True,
                duration_ms=duration_ms,
                timeout_seconds=timeout_seconds,
            )
        history = await review_session.history()
        return ChildReviewOutcome(
            result=result,
            write_ok=_review_write_ok(history),
            write_status=_review_write_status(history),
            write_error=_review_write_error(history),
            write_data=_review_write_data(history),
            visible_events=recording_sink.snapshot(),
            duration_ms=_duration_ms(started_at),
            timeout_seconds=timeout_seconds,
        )
    finally:
        await runtime.aclose()


def _latest_evolution_write_message(history: list[Message]) -> Message | None:
    for message in reversed(history):
        if message.role == "tool" and message.name == REVIEWER_TOOL_NAME:
            return message
    return None


def _review_write_ok(history: list[Message]) -> bool:
    message = _latest_evolution_write_message(history)
    if message is None:
        return False
    return bool(message.metadata.get("ok") is True)


def _review_write_status(history: list[Message]) -> str | None:
    message = _latest_evolution_write_message(history)
    if message is None:
        return None
    data = message.metadata.get("data")
    if isinstance(data, dict):
        status = data.get("status")
        if isinstance(status, str):
            return status
    return None


def _review_write_error(history: list[Message]) -> str | None:
    message = _latest_evolution_write_message(history)
    if message is None:
        return "evolution_write was not called"
    error = message.metadata.get("error_message")
    if isinstance(error, str) and error.strip():
        return error
    data = message.metadata.get("data")
    if isinstance(data, dict):
        maybe_error = data.get("error")
        if isinstance(maybe_error, str) and maybe_error.strip():
            return maybe_error
    if message.metadata.get("ok") is True:
        return None
    return "evolution_write did not report success"


def _review_write_data(history: list[Message]) -> dict[str, object] | None:
    message = _latest_evolution_write_message(history)
    if message is None:
        return None
    data = message.metadata.get("data")
    if isinstance(data, dict):
        return dict(data)
    return None


def _infer_review_run_id(events: tuple[Event, ...], *, fallback: str) -> str:
    for event in reversed(events):
        if event.run_id:
            return event.run_id
    return fallback


def _duration_ms(started_at: float) -> int:
    return max(0, int((time.perf_counter() - started_at) * 1000))
