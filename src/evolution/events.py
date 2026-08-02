"""Evolution runtime event payload definitions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EvolutionReviewStartedPayload:
    review_id: str
    session_id: str
    timeout_seconds: float | None
    user_turn_count: int | None = None
    included_turns: tuple[int, ...] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "review_id": self.review_id,
            "session_id": self.session_id,
            "timeout_seconds": self.timeout_seconds,
        }
        if self.user_turn_count is not None:
            payload["user_turn_count"] = self.user_turn_count
        if self.included_turns is not None:
            payload["included_turns"] = list(self.included_turns)
        return payload


@dataclass(frozen=True)
class EvolutionReviewCompletedPayload:
    review_id: str
    review_run_id: str
    session_id: str
    write_status: str | None
    duration_ms: int
    timeout_hit: bool
    timeout_seconds: float | None
    nutrients_written: Any = None
    written_nutrient_ids: Any = None
    review_summary: Any = None
    nutrient_summaries: Any = None
    timed_out_after_write: bool = False

    @classmethod
    def from_child_outcome(
        cls,
        *,
        review_id: str,
        review_run_id: str,
        session_id: str,
        outcome: Any,
    ) -> EvolutionReviewCompletedPayload:
        write_data = outcome.write_data if isinstance(outcome.write_data, dict) else {}
        return cls(
            review_id=review_id,
            review_run_id=review_run_id,
            session_id=session_id,
            write_status=outcome.write_status,
            duration_ms=outcome.duration_ms,
            timeout_hit=outcome.timed_out,
            timeout_seconds=outcome.timeout_seconds,
            nutrients_written=write_data.get("nutrients_written"),
            written_nutrient_ids=write_data.get("written_nutrient_ids"),
            review_summary=write_data.get("review_summary"),
            nutrient_summaries=write_data.get("nutrient_summaries"),
            timed_out_after_write=outcome.timed_out,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "review_run_id": self.review_run_id,
            "session_id": self.session_id,
            "write_status": self.write_status,
            "duration_ms": self.duration_ms,
            "timeout_hit": self.timeout_hit,
            "timeout_seconds": self.timeout_seconds,
            "nutrients_written": self.nutrients_written,
            "written_nutrient_ids": self.written_nutrient_ids,
            "review_summary": self.review_summary,
            "nutrient_summaries": self.nutrient_summaries,
            "timed_out_after_write": self.timed_out_after_write,
        }


@dataclass(frozen=True)
class EvolutionReviewFailedPayload:
    review_id: str
    session_id: str
    error_kind: str
    review_run_id: str | None = None
    message: str | None = None
    child_status: str | None = None
    write_status: str | None = None
    duration_ms: int | None = None
    timeout_hit: bool | None = None
    timeout_seconds: float | None = None
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "review_id": self.review_id,
            "session_id": self.session_id,
            "error_kind": self.error_kind,
        }
        _put_if_not_none(payload, "review_run_id", self.review_run_id)
        _put_if_not_none(payload, "message", self.message)
        _put_if_not_none(payload, "child_status", self.child_status)
        _put_if_not_none(payload, "write_status", self.write_status)
        _put_if_not_none(payload, "duration_ms", self.duration_ms)
        _put_if_not_none(payload, "timeout_hit", self.timeout_hit)
        _put_if_not_none(payload, "timeout_seconds", self.timeout_seconds)
        _put_if_not_none(payload, "error", self.error)
        return payload


@dataclass(frozen=True)
class EvolutionReviewDrainTimeoutPayload:
    pending_review_ids: tuple[str, ...]
    timeout_seconds: float

    @classmethod
    def from_review_ids(
        cls,
        *,
        pending_review_ids: Sequence[str],
        timeout_seconds: float,
    ) -> EvolutionReviewDrainTimeoutPayload:
        return cls(
            pending_review_ids=tuple(pending_review_ids),
            timeout_seconds=timeout_seconds,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "pending_review_ids": list(self.pending_review_ids),
            "timeout_seconds": self.timeout_seconds,
        }


def _put_if_not_none(payload: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        payload[key] = value
