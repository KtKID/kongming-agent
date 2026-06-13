"""Self evolution 内容层落盘。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import quote

from core.contracts import Event, EventSink
from evolution.models import (
    ApplyJob,
    DecisionApplyMode,
    DecisionApplyStatus,
    DecisionRecord,
    DecisionSummary,
    EvolutionNutrient,
    ReviewNoticeSnapshot,
    ReviewResult,
    ReviewWritePayload,
)
from evolution.state_store import EvolutionStateStore
from infrastructure.config.paths import resolve_kongming_path


def resolve_evolution_root(raw: str, *, kongming_home: Path | None = None) -> Path:
    return resolve_kongming_path(raw, kongming_home=kongming_home)


@dataclass(frozen=True)
class EvolutionWriteOutcome:
    review_path: str
    nutrients_written: int
    nutrients_skipped: int
    written_nutrient_ids: tuple[str, ...]
    status: str


class EvolutionStore:
    """统一管理 ``<kongming_home>/evolution`` 目录。"""

    def __init__(
        self,
        *,
        root_dir: Path,
        state_store: EvolutionStateStore,
        event_sinks: tuple[EventSink, ...] = (),
    ) -> None:
        self._root_dir = root_dir.resolve()
        self._reviews_dir = self._root_dir / "reviews"
        self._decisions_dir = self._root_dir / "decisions"
        self._apply_jobs_dir = self._root_dir / "apply-jobs"
        self._queue_path = self._root_dir / "evolution-nutrients.jsonl"
        self._state_store = state_store
        self._event_sinks = event_sinks
        self._lock = asyncio.Lock()

    async def write_review(self, payload: ReviewWritePayload) -> EvolutionWriteOutcome:
        async with self._lock:
            return await asyncio.to_thread(self._write_review_sync, payload)

    def _write_review_sync(self, payload: ReviewWritePayload) -> EvolutionWriteOutcome:
        self._root_dir.mkdir(parents=True, exist_ok=True)
        self._reviews_dir.mkdir(parents=True, exist_ok=True)
        review_result = payload.review_result
        review_path = self._reviews_dir / f"{review_result.run_id}.json"

        if review_path.exists():
            return EvolutionWriteOutcome(
                review_path=str(review_path),
                nutrients_written=0,
                nutrients_skipped=len(review_result.nutrients),
                written_nutrient_ids=(),
                status="already_exists",
            )

        existing_ids = self._load_existing_nutrient_ids()
        unique_nutrients: list[EvolutionNutrient] = []
        skipped_count = 0
        for nutrient in review_result.nutrients:
            if nutrient.nutrient_id in existing_ids:
                skipped_count += 1
                continue
            unique_nutrients.append(nutrient)
            existing_ids.add(nutrient.nutrient_id)

        record = {
            "version": 1,
            "run_id": review_result.run_id,
            "session_id": review_result.session_id,
            "status": "completed",
            "trigger_reason": payload.trigger_reason,
            "transcript_window": {
                "included_turns": list(payload.transcript_window.included_turns)
                if payload.transcript_window is not None
                else [],
                "summary": payload.transcript_window.summary
                if payload.transcript_window is not None
                else None,
            },
            "result": review_result.to_dict(),
        }
        tmp_path = review_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(review_path)

        if unique_nutrients:
            with self._queue_path.open("a", encoding="utf-8") as handle:
                for nutrient in unique_nutrients:
                    handle.write(json.dumps(nutrient.to_dict(), ensure_ascii=False) + "\n")

        return EvolutionWriteOutcome(
            review_path=str(review_path),
            nutrients_written=len(unique_nutrients),
            nutrients_skipped=skipped_count,
            written_nutrient_ids=tuple(nutrient.nutrient_id for nutrient in unique_nutrients),
            status="written",
        )

    async def list_reviews_for_session(self, session_id: str) -> tuple[ReviewResult, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._list_reviews_for_session_sync, session_id)

    async def read_decision(self, review_id: str) -> DecisionRecord | None:
        async with self._lock:
            return await asyncio.to_thread(self._read_decision_sync, review_id)

    async def write_decision(self, record: DecisionRecord) -> DecisionRecord:
        async with self._lock:
            return await asyncio.to_thread(self._write_decision_sync, record)

    async def read_apply_job(self, job_id: str) -> ApplyJob | None:
        async with self._lock:
            return await asyncio.to_thread(self._read_apply_job_sync, job_id)

    async def write_apply_job(self, job: ApplyJob) -> ApplyJob:
        async with self._lock:
            return await asyncio.to_thread(self._write_apply_job_sync, job)

    async def list_recoverable_apply_jobs(self) -> tuple[ApplyJob, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._list_recoverable_apply_jobs_sync)

    async def read_review(self, run_id: str) -> ReviewResult | None:
        async with self._lock:
            return await asyncio.to_thread(self._read_review_sync, run_id)

    async def record_apply_result(
        self,
        *,
        review_id: str,
        nutrient_id: str,
        applied_status: DecisionApplyStatus,
        applied_path: str | None = None,
        applied_mode: DecisionApplyMode | None = None,
        applied_at_ms: int | None = None,
        applied_error: str | None = None,
    ) -> DecisionRecord:
        async with self._lock:
            return await asyncio.to_thread(
                self._record_apply_result_sync,
                review_id,
                nutrient_id,
                applied_status,
                applied_path,
                applied_mode,
                applied_at_ms,
                applied_error,
            )

    async def list_notice_snapshots_for_session(
        self,
        session_id: str,
    ) -> tuple[ReviewNoticeSnapshot, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._list_notice_snapshots_for_session_sync, session_id)

    def _load_existing_nutrient_ids(self) -> set[str]:
        if not self._queue_path.exists():
            return set()
        seen: set[str] = set()
        with self._queue_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                nutrient_id = data.get("nutrient_id")
                if isinstance(nutrient_id, str) and nutrient_id:
                    seen.add(nutrient_id)
        return seen

    async def mark_review_result(
        self,
        *,
        session_id: str,
        run_id: str,
        status: str,
        reviewed_at_ms: int,
        nutrient_ids: tuple[str, ...],
    ) -> None:
        await self._state_store.mark_review_result(
            session_id=session_id,
            run_id=run_id,
            status=status,
            reviewed_at_ms=reviewed_at_ms,
            nutrient_ids=nutrient_ids,
        )

    async def emit_nutrient_events(
        self,
        *,
        run_id: str,
        nutrients: tuple[EvolutionNutrient, ...],
    ) -> None:
        for nutrient in nutrients:
            event = Event(
                kind="evolution.nutrient_written",
                run_id=run_id,
                payload={
                    "nutrient_id": nutrient.nutrient_id,
                    "kind": nutrient.kind,
                    "title": nutrient.title,
                    "confidence": nutrient.confidence,
                    "suggested_target": nutrient.suggested_target,
                },
            )
            for sink in self._event_sinks:
                await sink.emit(event)

    def _list_reviews_for_session_sync(self, session_id: str) -> tuple[ReviewResult, ...]:
        if not self._reviews_dir.exists():
            return ()
        reviews: list[ReviewResult] = []
        for path in sorted(self._reviews_dir.glob("*.json")):
            result = self._read_review_result_from_path(path)
            if result is None or result.session_id != session_id:
                continue
            reviews.append(result)
        reviews.sort(key=lambda review: review.reviewed_at_ms)
        return tuple(reviews)

    def _read_decision_sync(self, review_id: str) -> DecisionRecord | None:
        path = self._decision_path(review_id)
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("decision root must be an object")
        return DecisionRecord.from_dict(raw)

    def _write_decision_sync(self, record: DecisionRecord) -> DecisionRecord:
        self._root_dir.mkdir(parents=True, exist_ok=True)
        self._decisions_dir.mkdir(parents=True, exist_ok=True)
        normalized = self._recompute_decision_summary(self._normalize_decision_record(record))
        path = self._decision_path(normalized.review_id)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(normalized.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(path)
        return normalized

    def _read_apply_job_sync(self, job_id: str) -> ApplyJob | None:
        path = self._apply_job_path(job_id)
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("apply job root must be an object")
        return ApplyJob.from_dict(raw)

    def _write_apply_job_sync(self, job: ApplyJob) -> ApplyJob:
        self._root_dir.mkdir(parents=True, exist_ok=True)
        self._apply_jobs_dir.mkdir(parents=True, exist_ok=True)
        path = self._apply_job_path(job.job_id)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(job.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(path)
        return job

    def _list_recoverable_apply_jobs_sync(self) -> tuple[ApplyJob, ...]:
        if not self._apply_jobs_dir.exists():
            return ()
        jobs: list[ApplyJob] = []
        for path in sorted(self._apply_jobs_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            try:
                job = ApplyJob.from_dict(raw)
            except ValueError:
                continue
            if job.status in {"pending", "running"}:
                jobs.append(job)
        jobs.sort(key=lambda item: item.updated_at_ms or item.created_at_ms)
        return tuple(jobs)

    def _read_review_sync(self, run_id: str) -> ReviewResult | None:
        return self._read_review_result_from_path(self._reviews_dir / f"{run_id}.json")

    def _record_apply_result_sync(
        self,
        review_id: str,
        nutrient_id: str,
        applied_status: DecisionApplyStatus,
        applied_path: str | None,
        applied_mode: DecisionApplyMode | None,
        applied_at_ms: int | None,
        applied_error: str | None,
    ) -> DecisionRecord:
        record = self._read_decision_sync(review_id)
        if record is None:
            raise ValueError(f"decision record not found: {review_id}")
        updated_at_ms = applied_at_ms if applied_at_ms is not None else int(time.time() * 1000)
        items = list(record.items)
        found = False
        for index, item in enumerate(items):
            if item.nutrient_id != nutrient_id:
                continue
            items[index] = item.with_apply_result(
                applied_status=applied_status,
                applied_path=applied_path,
                applied_mode=applied_mode,
                applied_at_ms=updated_at_ms,
                applied_error=applied_error,
            )
            found = True
            break
        if not found:
            raise ValueError(f"decision item not found: {nutrient_id}")
        return self._write_decision_sync(replace(record, items=tuple(items)))

    def _list_notice_snapshots_for_session_sync(
        self,
        session_id: str,
    ) -> tuple[ReviewNoticeSnapshot, ...]:
        reviews = self._list_reviews_for_session_sync(session_id)
        snapshots: list[ReviewNoticeSnapshot] = []
        for review in reviews:
            decision = self._read_decision_sync(self._review_id_for_run(review.run_id))
            snapshots.append(self._build_notice_snapshot(review, decision))
        snapshots.sort(key=lambda item: item.reviewed_at_ms)
        return tuple(snapshots)

    def _decision_path(self, review_id: str) -> Path:
        return self._decisions_dir / f"{quote(review_id, safe='')}.json"

    def _apply_job_path(self, job_id: str) -> Path:
        return self._apply_jobs_dir / f"{quote(job_id, safe='')}.json"

    def _read_review_result_from_path(self, path: Path) -> ReviewResult | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        result_raw = raw.get("result")
        if not isinstance(result_raw, dict):
            return None
        try:
            return ReviewResult.from_dict(result_raw)
        except ValueError:
            return None

    def _recompute_decision_summary(self, record: DecisionRecord) -> DecisionRecord:
        accepted_memory = 0
        accepted_skill = 0
        ignored = 0
        for item in record.items:
            if item.decision == "accept_memory":
                accepted_memory += 1
            elif item.decision == "accept_skill":
                accepted_skill += 1
            else:
                ignored += 1
        total = record.summary.total or len(record.items)
        handled = accepted_memory + accepted_skill + ignored
        return DecisionRecord(
            review_id=record.review_id,
            session_id=record.session_id,
            run_id=record.run_id,
            summary=DecisionSummary(
                total=total,
                accepted_memory=accepted_memory,
                accepted_skill=accepted_skill,
                ignored=ignored,
                pending=max(total - handled, 0),
            ),
            items=record.items,
        )

    def _normalize_decision_record(self, record: DecisionRecord) -> DecisionRecord:
        normalized_items = tuple(item.normalized_for_storage() for item in record.items)
        return replace(record, items=normalized_items)

    def _build_notice_snapshot(
        self,
        review: ReviewResult,
        decision: DecisionRecord | None,
    ) -> ReviewNoticeSnapshot:
        total = len(review.nutrients)
        summary = (
            decision.summary
            if decision is not None
            else DecisionSummary(
                total=total,
                accepted_memory=0,
                accepted_skill=0,
                ignored=0,
                pending=total,
            )
        )
        handled = summary.accepted_memory + summary.accepted_skill + summary.ignored
        applied_written = 0
        applied_skipped = 0
        applied_failed = 0
        applied_pending = 0
        if decision is not None:
            for item in decision.items:
                if item.decision == "ignore":
                    continue
                if item.applied_status == "written":
                    applied_written += 1
                elif item.applied_status == "skipped":
                    applied_skipped += 1
                elif item.applied_status == "failed":
                    applied_failed += 1
                else:
                    applied_pending += 1

        if applied_written or applied_skipped or applied_failed or applied_pending:
            parts = [f"已写入 {applied_written}/{total} 条进化养料"]
            if applied_skipped:
                parts.append(f"已命中 {applied_skipped} 条")
            if applied_failed:
                parts.append(f"失败 {applied_failed} 条")
            if applied_pending:
                parts.append(f"待写入 {applied_pending} 条")
            message = "，".join(parts)
        else:
            message = (
                f"发现 {total} 条进化养料"
                if handled == 0
                else f"已处理 {handled}/{total} 条进化养料"
            )
        return ReviewNoticeSnapshot(
            review_id=self._review_id_for_run(review.run_id),
            run_id=review.run_id,
            session_id=review.session_id,
            reviewed_at_ms=review.reviewed_at_ms,
            nutrient_count=total,
            handled_count=handled,
            pending_count=max(total - handled, 0),
            accepted_memory_count=summary.accepted_memory,
            accepted_skill_count=summary.accepted_skill,
            ignored_count=summary.ignored,
            status="completed",
            title="进化复盘",
            message=message,
            icon="success",
            details={
                "review_id": self._review_id_for_run(review.run_id),
                "review_run_id": review.run_id,
                "session_id": review.session_id,
                "write_status": "written",
                "nutrient_count": total,
                "handled_count": handled,
                "pending_count": max(total - handled, 0),
                "accepted_memory_count": summary.accepted_memory,
                "accepted_skill_count": summary.accepted_skill,
                "ignored_count": summary.ignored,
                "applied_written_count": applied_written,
                "applied_skipped_count": applied_skipped,
                "applied_failed_count": applied_failed,
                "applied_pending_count": applied_pending,
            },
        )

    @staticmethod
    def _review_id_for_run(run_id: str) -> str:
        return f"evo-review:{run_id}"
