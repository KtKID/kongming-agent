"""Self evolution v0.1.9 数据协议。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, cast

from core.clock import now_epoch_ms
from core.message import Message

TranscriptRole = Literal["user", "assistant", "tool", "system"]
NutrientKind = Literal["memory", "workflow", "error"]
SuggestedTarget = Literal["memory", "skill", "errorbook"]
DecisionValue = Literal["accept_memory", "accept_skill", "ignore"]
DecisionTarget = Literal["memory", "skill"]
DecisionApplyStatus = Literal["pending", "written", "skipped", "failed"]
DecisionApplyMode = Literal["append", "update", "create", "ignore"]
ApplyJobStatus = Literal["pending", "running", "finished", "failed"]
MAX_MANUAL_REVIEW_FOCUS_CHARS = 500


class ManualReviewQueueStatus(StrEnum):
    """显式审查请求的幂等排队结果。"""

    QUEUED = "queued"
    ALREADY_QUEUED = "already_queued"


class EvolutionReviewTrigger(StrEnum):
    """进化审查的封闭触发来源。"""

    MANUAL_TOOL = "manual_tool"
    MANUAL_COMMAND = "manual_command"
    CADENCE = "cadence"
    CADENCE_EVOLUTION_MANAGER = "cadence_evolution_manager"


@dataclass(frozen=True)
class ManualReviewRequest:
    """当前主 run 内等待 after-run 消费的显式审查请求。"""

    session_id: str
    run_id: str
    focus: str | None
    requested_at_ms: int


@dataclass(frozen=True)
class EvolutionReviewPlan:
    """手动与自动触发合流后生成的单次审查计划。"""

    trigger: EvolutionReviewTrigger
    focus: str | None = None


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _require_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _require_float(data: dict[str, Any], key: str) -> float:
    value = data.get(key)
    if not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number")
    return float(value)


def _string_tuple(value: Any, *, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{key} must be a list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{key} must contain only strings")
        out.append(item)
    return tuple(out)


def _int_tuple(value: Any, *, key: str) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{key} must be a list of integers")
    out: list[int] = []
    for item in value:
        if not isinstance(item, int):
            raise ValueError(f"{key} must contain only integers")
        out.append(item)
    return tuple(out)


@dataclass(frozen=True)
class TranscriptMessage:
    turn: int
    role: TranscriptRole
    content: str
    tool_name: str | None = None

    def to_message(self, index: int) -> Message:
        role_label: str = self.role
        if self.role == "tool" and self.tool_name:
            role_label = f"tool:{self.tool_name}"
        rendered = f"[turn {self.turn}][{role_label}] {self.content}"
        if self.role == "system":
            return Message.system(rendered)
        if self.role == "assistant":
            return Message.assistant(rendered)
        if self.role == "tool":
            return Message.assistant(rendered, metadata={"evolution_transcript_role": "tool"})
        return Message.user(rendered)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "role": self.role,
            "content": self.content,
            "tool_name": self.tool_name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TranscriptMessage:
        turn = _require_int(data, "turn")
        role = data.get("role")
        if role not in {"user", "assistant", "tool", "system"}:
            raise ValueError("role must be one of user/assistant/tool/system")
        content = _require_str(data, "content")
        tool_name = data.get("tool_name")
        if tool_name is not None and not isinstance(tool_name, str):
            raise ValueError("tool_name must be a string when provided")
        return cls(turn=turn, role=role, content=content, tool_name=tool_name)


@dataclass(frozen=True)
class TranscriptWindow:
    session_id: str
    run_id: str
    user_turn_count: int
    included_turns: tuple[int, ...]
    messages: tuple[TranscriptMessage, ...]
    final_message: str | None
    tool_call_count: int
    summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "run_id": self.run_id,
            "user_turn_count": self.user_turn_count,
            "included_turns": list(self.included_turns),
            "messages": [message.to_dict() for message in self.messages],
            "final_message": self.final_message,
            "tool_call_count": self.tool_call_count,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TranscriptWindow:
        messages_raw = data.get("messages")
        messages: tuple[TranscriptMessage, ...] = ()
        if isinstance(messages_raw, list):
            parsed_messages: list[TranscriptMessage] = []
            for item in messages_raw:
                if not isinstance(item, dict):
                    continue
                try:
                    parsed_messages.append(TranscriptMessage.from_dict(item))
                except ValueError:
                    continue
            messages = tuple(parsed_messages)
        final_message = data.get("final_message")
        if final_message is not None and not isinstance(final_message, str):
            final_message = None
        summary = data.get("summary")
        if summary is not None and not isinstance(summary, str):
            summary = None
        user_turn_count = data.get("user_turn_count", 0)
        tool_call_count = data.get("tool_call_count", 0)
        return cls(
            session_id=_require_str(data, "session_id"),
            run_id=_require_str(data, "run_id"),
            user_turn_count=user_turn_count if isinstance(user_turn_count, int) else 0,
            included_turns=_int_tuple(data.get("included_turns"), key="included_turns"),
            messages=messages,
            final_message=final_message,
            tool_call_count=tool_call_count if isinstance(tool_call_count, int) else 0,
            summary=summary,
        )


@dataclass(frozen=True)
class EvolutionNutrient:
    nutrient_id: str
    kind: NutrientKind
    title: str
    content: str
    summary: str
    confidence: float
    evidence_turns: tuple[int, ...]
    source_run_id: str
    source_session_id: str
    suggested_target: SuggestedTarget | None = None
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "nutrient_id": self.nutrient_id,
            "kind": self.kind,
            "title": self.title,
            "content": self.content,
            "summary": self.summary,
            "confidence": self.confidence,
            "evidence_turns": list(self.evidence_turns),
            "source_run_id": self.source_run_id,
            "source_session_id": self.source_session_id,
            "suggested_target": self.suggested_target,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvolutionNutrient:
        kind = data.get("kind")
        if kind not in {"memory", "workflow", "error"}:
            raise ValueError("kind must be one of memory/workflow/error")
        suggested_target = data.get("suggested_target")
        if suggested_target is not None and suggested_target not in {
            "memory",
            "skill",
            "errorbook",
        }:
            raise ValueError("suggested_target must be one of memory/skill/errorbook")
        return cls(
            nutrient_id=_require_str(data, "nutrient_id"),
            kind=kind,
            title=_require_str(data, "title"),
            content=_require_str(data, "content"),
            summary=_require_str(data, "summary"),
            confidence=_require_float(data, "confidence"),
            evidence_turns=_int_tuple(data.get("evidence_turns"), key="evidence_turns"),
            source_run_id=_require_str(data, "source_run_id"),
            source_session_id=_require_str(data, "source_session_id"),
            suggested_target=suggested_target,
            tags=_string_tuple(data.get("tags"), key="tags"),
        )


@dataclass(frozen=True)
class ReviewResult:
    run_id: str
    session_id: str
    reviewed_at_ms: int
    review_summary: str
    nutrients: tuple[EvolutionNutrient, ...] = ()
    skip_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "reviewed_at_ms": self.reviewed_at_ms,
            "review_summary": self.review_summary,
            "nutrients": [nutrient.to_dict() for nutrient in self.nutrients],
            "skip_reasons": list(self.skip_reasons),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewResult:
        nutrients_raw = data.get("nutrients", [])
        if not isinstance(nutrients_raw, list):
            raise ValueError("nutrients must be a list")
        parsed_nutrients: list[EvolutionNutrient] = []
        for item in nutrients_raw:
            if not isinstance(item, dict):
                continue
            try:
                parsed_nutrients.append(EvolutionNutrient.from_dict(item))
            except ValueError:
                continue
        return cls(
            run_id=_require_str(data, "run_id"),
            session_id=_require_str(data, "session_id"),
            reviewed_at_ms=_require_int(data, "reviewed_at_ms"),
            review_summary=_require_str(data, "review_summary"),
            nutrients=tuple(parsed_nutrients),
            skip_reasons=_string_tuple(data.get("skip_reasons"), key="skip_reasons"),
        )


@dataclass(frozen=True)
class ReviewWritePayload:
    review_result: ReviewResult
    transcript_window: TranscriptWindow | None = None
    trigger_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_result": self.review_result.to_dict(),
            "transcript_window": (
                self.transcript_window.to_dict() if self.transcript_window is not None else None
            ),
            "trigger_reason": self.trigger_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewWritePayload:
        transcript_raw = data.get("transcript_window")
        transcript_window: TranscriptWindow | None = None
        if transcript_raw is not None and isinstance(transcript_raw, dict):
            try:
                transcript_window = TranscriptWindow.from_dict(transcript_raw)
            except ValueError:
                transcript_window = None

        review_raw = data.get("review_result")
        if review_raw is None:
            review_raw = {
                "run_id": data.get("run_id"),
                "session_id": data.get("session_id"),
                "reviewed_at_ms": data.get("reviewed_at_ms"),
                "review_summary": data.get("review_summary"),
                "nutrients": data.get("nutrients", []),
                "skip_reasons": data.get("skip_reasons", []),
            }
        if not isinstance(review_raw, dict):
            raise ValueError("review_result must be an object")
        review_normalized = _normalize_review_result_payload(review_raw, transcript_window, data)
        trigger_reason = data.get("trigger_reason")
        if trigger_reason is not None and not isinstance(trigger_reason, str):
            raise ValueError("trigger_reason must be a string when provided")
        return cls(
            review_result=ReviewResult.from_dict(review_normalized),
            transcript_window=transcript_window,
            trigger_reason=trigger_reason,
        )


@dataclass(frozen=True)
class DecisionItem:
    nutrient_id: str
    decision: DecisionValue
    target: DecisionTarget | None = None
    decided_at_ms: int = 0
    applied_status: DecisionApplyStatus = "pending"
    applied_path: str | None = None
    applied_mode: DecisionApplyMode | None = None
    applied_at_ms: int | None = None
    applied_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "nutrient_id": self.nutrient_id,
            "decision": self.decision,
            "target": self.target,
            "decided_at_ms": self.decided_at_ms,
            "applied_status": self.applied_status,
            "applied_path": self.applied_path,
            "applied_mode": self.applied_mode,
            "applied_at_ms": self.applied_at_ms,
            "applied_error": self.applied_error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DecisionItem:
        decision = data.get("decision")
        if decision not in {"accept_memory", "accept_skill", "ignore"}:
            raise ValueError("decision must be one of accept_memory/accept_skill/ignore")
        target = data.get("target")
        if target is not None and target not in {"memory", "skill"}:
            raise ValueError("target must be one of memory/skill when provided")
        decided_at_ms = data.get("decided_at_ms", 0)
        if not isinstance(decided_at_ms, int):
            raise ValueError("decided_at_ms must be an integer")
        applied_status_raw = data.get("applied_status")
        if applied_status_raw is None:
            applied_status_raw = "skipped" if decision == "ignore" else "pending"
        if applied_status_raw not in {"pending", "written", "skipped", "failed"}:
            raise ValueError("applied_status must be one of pending/written/skipped/failed")
        applied_status = cast(DecisionApplyStatus, applied_status_raw)
        applied_path = data.get("applied_path")
        if applied_path is not None and not isinstance(applied_path, str):
            raise ValueError("applied_path must be a string when provided")
        applied_mode = data.get("applied_mode")
        if applied_mode is None and decision == "ignore":
            applied_mode = "ignore"
        if applied_mode is not None and applied_mode not in {
            "append",
            "update",
            "create",
            "ignore",
        }:
            raise ValueError("applied_mode must be one of append/update/create/ignore")
        applied_at_ms = data.get("applied_at_ms")
        if applied_at_ms is None and decision == "ignore" and applied_status == "skipped":
            applied_at_ms = decided_at_ms
        if applied_at_ms is not None and not isinstance(applied_at_ms, int):
            raise ValueError("applied_at_ms must be an integer when provided")
        applied_error = data.get("applied_error")
        if applied_error is not None and not isinstance(applied_error, str):
            raise ValueError("applied_error must be a string when provided")
        return cls(
            nutrient_id=_require_str(data, "nutrient_id"),
            decision=decision,
            target=target,
            decided_at_ms=decided_at_ms,
            applied_status=applied_status,
            applied_path=applied_path,
            applied_mode=applied_mode,
            applied_at_ms=applied_at_ms,
            applied_error=applied_error,
        )

    def normalized_for_storage(self) -> DecisionItem:
        if self.decision != "ignore":
            return self
        if self.applied_status != "pending":
            return self
        return DecisionItem(
            nutrient_id=self.nutrient_id,
            decision=self.decision,
            target=self.target,
            decided_at_ms=self.decided_at_ms,
            applied_status="skipped",
            applied_path=self.applied_path,
            applied_mode=self.applied_mode or "ignore",
            applied_at_ms=self.applied_at_ms
            if self.applied_at_ms is not None
            else self.decided_at_ms,
            applied_error=self.applied_error,
        )

    def with_apply_result(
        self,
        *,
        applied_status: DecisionApplyStatus,
        applied_path: str | None = None,
        applied_mode: DecisionApplyMode | None = None,
        applied_at_ms: int | None = None,
        applied_error: str | None = None,
    ) -> DecisionItem:
        return DecisionItem(
            nutrient_id=self.nutrient_id,
            decision=self.decision,
            target=self.target,
            decided_at_ms=self.decided_at_ms,
            applied_status=applied_status,
            applied_path=applied_path,
            applied_mode=applied_mode,
            applied_at_ms=applied_at_ms,
            applied_error=applied_error,
        ).normalized_for_storage()


@dataclass(frozen=True)
class DecisionSummary:
    total: int
    accepted_memory: int
    accepted_skill: int
    ignored: int
    pending: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "accepted_memory": self.accepted_memory,
            "accepted_skill": self.accepted_skill,
            "ignored": self.ignored,
            "pending": self.pending,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DecisionSummary:
        return cls(
            total=int(data.get("total", 0)),
            accepted_memory=int(data.get("accepted_memory", 0)),
            accepted_skill=int(data.get("accepted_skill", 0)),
            ignored=int(data.get("ignored", 0)),
            pending=int(data.get("pending", 0)),
        )


@dataclass(frozen=True)
class DecisionRecord:
    review_id: str
    session_id: str
    run_id: str
    summary: DecisionSummary
    items: tuple[DecisionItem, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "summary": self.summary.to_dict(),
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DecisionRecord:
        summary_raw = data.get("summary")
        if summary_raw is None:
            summary_raw = {}
        if not isinstance(summary_raw, dict):
            raise ValueError("summary must be an object")
        items_raw = data.get("items", [])
        if not isinstance(items_raw, list):
            raise ValueError("items must be a list")
        parsed_items: list[DecisionItem] = []
        for item in items_raw:
            if not isinstance(item, dict):
                continue
            parsed_items.append(DecisionItem.from_dict(item))
        return cls(
            review_id=_require_str(data, "review_id"),
            session_id=_require_str(data, "session_id"),
            run_id=_require_str(data, "run_id"),
            summary=DecisionSummary.from_dict(summary_raw),
            items=tuple(parsed_items),
        )


@dataclass(frozen=True)
class ApplyJob:
    job_id: str
    review_id: str
    session_id: str
    run_id: str
    nutrient_id: str
    decision: DecisionValue
    target: DecisionTarget | None
    workspace_root: str
    status: ApplyJobStatus
    attempt_count: int
    artifact_path: str | None = None
    mode: DecisionApplyMode | None = None
    last_error: str | None = None
    created_at_ms: int = 0
    updated_at_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "review_id": self.review_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "nutrient_id": self.nutrient_id,
            "decision": self.decision,
            "target": self.target,
            "workspace_root": self.workspace_root,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "artifact_path": self.artifact_path,
            "mode": self.mode,
            "last_error": self.last_error,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApplyJob:
        decision = data.get("decision")
        if decision not in {"accept_memory", "accept_skill", "ignore"}:
            raise ValueError("decision must be one of accept_memory/accept_skill/ignore")
        target = data.get("target")
        if target is not None and target not in {"memory", "skill"}:
            raise ValueError("target must be one of memory/skill when provided")
        status = data.get("status")
        if status not in {"pending", "running", "finished", "failed"}:
            raise ValueError("status must be one of pending/running/finished/failed")
        attempt_count = data.get("attempt_count", 0)
        if not isinstance(attempt_count, int):
            raise ValueError("attempt_count must be an integer")
        artifact_path = data.get("artifact_path")
        if artifact_path is not None and not isinstance(artifact_path, str):
            raise ValueError("artifact_path must be a string when provided")
        mode = data.get("mode")
        if mode is not None and mode not in {"append", "update", "create", "ignore"}:
            raise ValueError("mode must be one of append/update/create/ignore")
        last_error = data.get("last_error")
        if last_error is not None and not isinstance(last_error, str):
            raise ValueError("last_error must be a string when provided")
        created_at_ms = data.get("created_at_ms", 0)
        if not isinstance(created_at_ms, int):
            raise ValueError("created_at_ms must be an integer")
        updated_at_ms = data.get("updated_at_ms", 0)
        if not isinstance(updated_at_ms, int):
            raise ValueError("updated_at_ms must be an integer")
        return cls(
            job_id=_require_str(data, "job_id"),
            review_id=_require_str(data, "review_id"),
            session_id=_require_str(data, "session_id"),
            run_id=_require_str(data, "run_id"),
            nutrient_id=_require_str(data, "nutrient_id"),
            decision=decision,
            target=target,
            workspace_root=_require_str(data, "workspace_root"),
            status=status,
            attempt_count=attempt_count,
            artifact_path=artifact_path,
            mode=mode,
            last_error=last_error,
            created_at_ms=created_at_ms,
            updated_at_ms=updated_at_ms,
        )

    def mark_running(self, *, updated_at_ms: int) -> ApplyJob:
        return ApplyJob(
            job_id=self.job_id,
            review_id=self.review_id,
            session_id=self.session_id,
            run_id=self.run_id,
            nutrient_id=self.nutrient_id,
            decision=self.decision,
            target=self.target,
            workspace_root=self.workspace_root,
            status="running",
            attempt_count=self.attempt_count + 1,
            artifact_path=self.artifact_path,
            mode=self.mode,
            last_error=None,
            created_at_ms=self.created_at_ms,
            updated_at_ms=updated_at_ms,
        )

    def mark_finished(
        self,
        *,
        artifact_path: str | None,
        mode: DecisionApplyMode | None,
        updated_at_ms: int,
    ) -> ApplyJob:
        return ApplyJob(
            job_id=self.job_id,
            review_id=self.review_id,
            session_id=self.session_id,
            run_id=self.run_id,
            nutrient_id=self.nutrient_id,
            decision=self.decision,
            target=self.target,
            workspace_root=self.workspace_root,
            status="finished",
            attempt_count=self.attempt_count,
            artifact_path=artifact_path,
            mode=mode,
            last_error=None,
            created_at_ms=self.created_at_ms,
            updated_at_ms=updated_at_ms,
        )

    def mark_failed(self, *, error: str, updated_at_ms: int) -> ApplyJob:
        return ApplyJob(
            job_id=self.job_id,
            review_id=self.review_id,
            session_id=self.session_id,
            run_id=self.run_id,
            nutrient_id=self.nutrient_id,
            decision=self.decision,
            target=self.target,
            workspace_root=self.workspace_root,
            status="failed",
            attempt_count=self.attempt_count,
            artifact_path=self.artifact_path,
            mode=self.mode,
            last_error=error,
            created_at_ms=self.created_at_ms,
            updated_at_ms=updated_at_ms,
        )


@dataclass(frozen=True)
class ReviewNoticeSnapshot:
    review_id: str
    run_id: str
    session_id: str
    reviewed_at_ms: int
    nutrient_count: int
    handled_count: int
    pending_count: int
    accepted_memory_count: int
    accepted_skill_count: int
    ignored_count: int
    status: Literal["completed"]
    title: str
    message: str
    icon: Literal["success"]
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "reviewed_at_ms": self.reviewed_at_ms,
            "nutrient_count": self.nutrient_count,
            "handled_count": self.handled_count,
            "pending_count": self.pending_count,
            "accepted_memory_count": self.accepted_memory_count,
            "accepted_skill_count": self.accepted_skill_count,
            "ignored_count": self.ignored_count,
            "status": self.status,
            "title": self.title,
            "message": self.message,
            "icon": self.icon,
            "details": dict(self.details),
        }


@dataclass
class SessionLearningState:
    run_count: int = 0
    user_turn_count: int = 0
    last_reviewed_run_id: str = ""
    last_review_at_ms: int = 0
    last_nutrient_id: str = ""
    last_nutrient_at_ms: int = 0
    last_review_status: str = "idle"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_count": self.run_count,
            "user_turn_count": self.user_turn_count,
            "last_reviewed_run_id": self.last_reviewed_run_id,
            "last_review_at_ms": self.last_review_at_ms,
            "last_nutrient_id": self.last_nutrient_id,
            "last_nutrient_at_ms": self.last_nutrient_at_ms,
            "last_review_status": self.last_review_status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionLearningState:
        return cls(
            run_count=int(data.get("run_count", 0)),
            user_turn_count=int(data.get("user_turn_count", 0)),
            last_reviewed_run_id=str(data.get("last_reviewed_run_id", "")),
            last_review_at_ms=int(data.get("last_review_at_ms", 0)),
            last_nutrient_id=str(data.get("last_nutrient_id", "")),
            last_nutrient_at_ms=int(data.get("last_nutrient_at_ms", 0)),
            last_review_status=str(data.get("last_review_status", "idle")),
        )


def _normalize_review_result_payload(
    review_raw: dict[str, Any],
    transcript_window: TranscriptWindow | None,
    root_data: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(review_raw)
    run_id = normalized.get("run_id") or root_data.get("run_id")
    session_id = normalized.get("session_id") or root_data.get("session_id")
    if transcript_window is not None:
        run_id = run_id or transcript_window.run_id
        session_id = session_id or transcript_window.session_id
    if run_id:
        normalized["run_id"] = run_id
    if session_id:
        normalized["session_id"] = session_id

    if not isinstance(normalized.get("reviewed_at_ms"), int):
        reviewed_at_ms = root_data.get("reviewed_at_ms")
        normalized["reviewed_at_ms"] = (
            reviewed_at_ms if isinstance(reviewed_at_ms, int) else now_epoch_ms()
        )
    if (
        not isinstance(normalized.get("review_summary"), str)
        or not normalized.get("review_summary", "").strip()
    ):
        count = len(normalized.get("nutrients", []) or [])
        normalized["review_summary"] = (
            root_data.get("review_summary")
            if isinstance(root_data.get("review_summary"), str)
            and root_data.get("review_summary", "").strip()
            else f"captured {count} evolution nutrients"
        )

    nutrients = normalized.get("nutrients", [])
    if isinstance(nutrients, list):
        updated: list[dict[str, Any]] = []
        for index, nutrient in enumerate(nutrients):
            if not isinstance(nutrient, dict):
                updated.append(nutrient)
                continue
            item = dict(nutrient)
            item["source_run_id"] = item.get("source_run_id") or normalized.get("run_id")
            item["source_session_id"] = item.get("source_session_id") or normalized.get(
                "session_id"
            )
            if not item.get("nutrient_id"):
                base = item.get("title") or item.get("kind") or "nutrient"
                slug = str(base).strip().lower().replace(" ", "-")
                item["nutrient_id"] = f"{normalized.get('run_id', 'review')}-{slug}-{index + 1}"
            updated.append(item)
        normalized["nutrients"] = updated

    return normalized
