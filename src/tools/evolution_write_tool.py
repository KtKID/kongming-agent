"""Evolution write tool。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from core.contracts import EventSink, ToolContext
from evolution.models import ReviewResult, ReviewWritePayload
from evolution.store import EvolutionStore
from tools.base import BaseBuiltinTool


class EvolutionWriteTool(BaseBuiltinTool):
    name = "evolution_write"
    description = (
        "Write evolution review records and evolution nutrients into .kongming/evolution/. "
        "Use this tool only from the dedicated evolution reviewer agent."
    )
    input_schema: dict[str, Any]

    def __init__(
        self,
        store: EvolutionStore,
        *,
        min_confidence: float,
        max_nutrients: int,
        event_sinks: Sequence[EventSink] = (),
    ) -> None:
        super().__init__()
        self.input_schema = {
            "type": "object",
            "properties": {
                "review_result": {"type": "object"},
                "transcript_window": {"type": "object"},
                "trigger_reason": {"type": "string"},
            },
        }
        self._store = store
        self._min_confidence = min_confidence
        self._max_nutrients = max_nutrients
        self._event_sinks = tuple(event_sinks)

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        payload = ReviewWritePayload.from_dict(self._apply_reviewer_fallback(args, ctx))
        filtered = self._filter_review_result(payload.review_result)
        normalized = ReviewWritePayload(
            review_result=filtered,
            transcript_window=payload.transcript_window,
            trigger_reason=payload.trigger_reason,
        )
        outcome = await self._store.write_review(normalized)
        await self._store.mark_review_result(
            session_id=filtered.session_id,
            run_id=filtered.run_id,
            status="written" if outcome.status == "written" else outcome.status,
            reviewed_at_ms=filtered.reviewed_at_ms,
            nutrient_ids=outcome.written_nutrient_ids,
        )
        written = tuple(
            nutrient
            for nutrient in filtered.nutrients
            if nutrient.nutrient_id in set(outcome.written_nutrient_ids)
        )
        if written:
            await self._store.emit_nutrient_events(run_id=filtered.run_id, nutrients=written)
        content = (
            f"evolution review stored at {outcome.review_path}; "
            f"wrote {outcome.nutrients_written} nutrients, skipped {outcome.nutrients_skipped}."
        )
        return content, {
            "success": True,
            "status": outcome.status,
            "review_path": outcome.review_path,
            "nutrients_written": outcome.nutrients_written,
            "nutrients_skipped": outcome.nutrients_skipped,
            "written_nutrient_ids": list(outcome.written_nutrient_ids),
        }

    def _filter_review_result(self, result: ReviewResult) -> ReviewResult:
        nutrients = tuple(
            nutrient for nutrient in result.nutrients if nutrient.confidence >= self._min_confidence
        )[: self._max_nutrients]
        return ReviewResult(
            run_id=result.run_id,
            session_id=result.session_id,
            reviewed_at_ms=result.reviewed_at_ms,
            review_summary=result.review_summary,
            nutrients=nutrients,
            skip_reasons=result.skip_reasons,
        )

    def _apply_reviewer_fallback(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> dict[str, Any]:
        payload = dict(args)
        recovered = _recover_parent_ids_from_review_session(ctx.session_id)
        if recovered is None:
            return payload
        session_id, run_id = recovered
        payload.setdefault("session_id", session_id)
        payload.setdefault("run_id", run_id)
        review_result = payload.get("review_result")
        if isinstance(review_result, dict):
            nested = dict(review_result)
            nested.setdefault("session_id", session_id)
            nested.setdefault("run_id", run_id)
            payload["review_result"] = nested
        return payload


def build_evolution_write_tool(
    store: EvolutionStore,
    *,
    min_confidence: float,
    max_nutrients: int,
    event_sinks: Sequence[EventSink] = (),
) -> EvolutionWriteTool:
    return EvolutionWriteTool(
        store,
        min_confidence=min_confidence,
        max_nutrients=max_nutrients,
        event_sinks=event_sinks,
    )


__all__ = ["EvolutionWriteTool", "build_evolution_write_tool"]


def _recover_parent_ids_from_review_session(session_id: str) -> tuple[str, str] | None:
    prefix = "evo-review-"
    if not session_id.startswith(prefix):
        return None
    body = session_id[len(prefix) :]
    parent_session_id, sep, run_tail = body.rpartition("-run-")
    if not sep or not parent_session_id or not run_tail:
        return None
    return parent_session_id, f"run-{run_tail}"
