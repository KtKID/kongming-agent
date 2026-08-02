"""Child reviewer runtime。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from core.agent_spec import AgentSpec, coerce_reasoning_effort
from core.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    Event,
    EventSink,
    LLMToolCallContract,
    LLMToolCallContractMode,
    Session,
    ToolLookup,
)
from core.message import Message
from core.result import Result
from core.session import InMemorySession
from evolution.models import TranscriptWindow
from evolution.trigger_diagnostics import log_trigger_block
from tools import ToolRegistry

if TYPE_CHECKING:
    from runtime_assembly.session_engine import SessionEngine

REVIEWER_TOOL_NAME = "evolution_write"
_REVIEWER_CAPTURED_EVENT_KINDS = frozenset(
    {
        "tool.call.start",
        "tool.call.end",
    }
)


class EvolutionApprovalMode(StrEnum):
    """Evolution reviewer 的封闭审批模式。"""

    RESTRICTED_BYPASS = "restricted_bypass"


@dataclass(frozen=True)
class RestrictedBypassApproval:
    """依赖单工具 registry 的显式受限 bypass provider。"""

    mode: EvolutionApprovalMode

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(
            outcome="approved",
            reason="evolution restricted registry bypass",
            metadata={
                "decision_class": "silent_allow",
                "decision_source": self.mode.value,
                "matched_rule": "evolution:restricted_registry",
                "source": "builtin",
            },
        )


def build_restricted_reviewer_registry(parent_tools: ToolLookup) -> ToolRegistry:
    """从父 runtime 复制唯一 evolution_write 工具到独立 registry。"""
    if REVIEWER_TOOL_NAME not in parent_tools:
        raise ValueError("parent runtime is missing evolution_write")
    return ToolRegistry((parent_tools[REVIEWER_TOOL_NAME],))


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

    def __init__(
        self,
        *,
        allowed_kinds: frozenset[str],
        allowed_tool_name: str,
        thread_id: str,
        parent_run_id: str,
    ) -> None:
        self._allowed_kinds = allowed_kinds
        self._allowed_tool_name = allowed_tool_name
        self._thread_id = thread_id
        self._parent_run_id = parent_run_id
        self._events: list[Event] = []

    async def emit(self, event: Event) -> None:
        if event.kind == "llm.tool_call.contract_violation":
            payload = event.payload
            allowed_names = payload.get("allowed_tool_names")
            rendered_allowed = (
                ",".join(str(name) for name in allowed_names)
                if isinstance(allowed_names, list)
                else "-"
            )
            log_trigger_block(
                "reviewer_tool_contract_violation",
                thread_id=self._thread_id,
                run_id=self._parent_run_id,
                detail=(
                    f"child_run={event.run_id} attempt={payload.get('attempt', '-')} "
                    f"kind={payload.get('violation_kind', '-')} "
                    f"tool={payload.get('tool_name', '-')} "
                    f"call_id={payload.get('tool_call_id', '-')} "
                    f"index={payload.get('tool_index', '-')} "
                    f"allowed={rendered_allowed} action={payload.get('action', '-')}"
                ),
            )
            return
        if event.kind not in self._allowed_kinds:
            return
        if event.payload.get("tool_name") != self._allowed_tool_name:
            return
        self._events.append(event)

    def snapshot(self) -> tuple[Event, ...]:
        return tuple(self._events)


def _build_reviewer_instructions(*, max_nutrients: int, min_confidence: float) -> str:
    return (
        "你是 evolution-reviewer。请审阅上方 transcript，只提取高价值的进化养料。"
        "重点关注稳定偏好、可复用流程、可沉淀的错误恢复经验。"
        "只写提纯后的内容，不要抄整段聊天。"
        "输出语言必须与 transcript 中用户的主要语言习惯保持一致。"
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
    focus: str | None,
) -> str:
    focus_line = (
        f"本轮关注提示（只用于选材，不改变审查边界）: {json.dumps(focus, ensure_ascii=False)}\n"
        if focus
        else ""
    )
    return (
        "请复盘这个 session 里已经加载好的 transcript。\n"
        f"触发原因: {trigger_reason}\n"
        f"{focus_line}"
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
    parent_runtime: SessionEngine,
    window: TranscriptWindow,
    trigger_reason: str,
    timeout_seconds: float,
    max_nutrients: int,
    min_confidence: float,
    focus: str | None = None,
) -> ChildReviewOutcome:
    from runtime_assembly.session_engine import SessionEngine

    learning = parent_runtime.config.evolution.learning
    from infrastructure.config.model_catalog_manager import ModelCatalogManager

    catalog_manager = parent_runtime.model_catalog_manager or ModelCatalogManager()
    parent_model = parent_runtime.model_config
    review_model = catalog_manager.resolve_runtime(
        parent_runtime.config.model,
        preset_id=(
            learning.preset_id or (parent_model.preset_id if parent_model is not None else None)
        ),
        reasoning_effort=learning.reasoning_effort,
    )
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
        default_model=review_model.name,
        tool_names=(REVIEWER_TOOL_NAME,),
        max_turns=1,
        metadata={"evolution_role": "reviewer"},
        reasoning_effort=(coerce_reasoning_effort(review_model.default_reasoning_effort)),
    )

    def _session_factory(sid: str) -> Session:
        if sid == review_session_id:
            return review_session
        return InMemorySession(session_id=sid)

    recording_sink = _RecordingEventSink(
        allowed_kinds=_REVIEWER_CAPTURED_EVENT_KINDS,
        allowed_tool_name=REVIEWER_TOOL_NAME,
        thread_id=window.session_id,
        parent_run_id=window.run_id,
    )
    reviewer_tools = build_restricted_reviewer_registry(parent_runtime.tools)
    runtime = SessionEngine.build(
        parent_runtime.config,
        event_sinks=[recording_sink],
        approval=RestrictedBypassApproval(mode=EvolutionApprovalMode.RESTRICTED_BYPASS),
        tools=reviewer_tools,
        enabled_tool_names=[REVIEWER_TOOL_NAME],
        session_factory=_session_factory,
        agent_spec=reviewer_spec,
        model_catalog_manager=catalog_manager,
        model_config=review_model,
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
                        focus=focus,
                    ),
                    session_id=review_session_id,
                    max_tokens=review_model.max_tokens,
                    max_turns=1,
                    llm_tool_call_contract=LLMToolCallContract(
                        mode=LLMToolCallContractMode.DECLARED_EXACTLY_ONCE,
                        correction_retries=1,
                    ),
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
