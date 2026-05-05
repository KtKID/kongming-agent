"""Apply job execution and recovery for self evolution."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from config_loader.models import Config
from evolution.memory_materializer import MemoryMaterializer
from evolution.models import (
    ApplyJob,
    DecisionApplyStatus,
    DecisionItem,
    DecisionRecord,
    EvolutionNutrient,
)
from evolution.skill_materializer import materialize_skill
from evolution.state_store import EvolutionStateStore
from evolution.store import EvolutionStore, resolve_evolution_root
from memory import MemoryStore


@dataclass(frozen=True)
class ApplyExecutionResult:
    job: ApplyJob
    decision_record: DecisionRecord


def build_apply_job(
    *,
    review_id: str,
    session_id: str,
    run_id: str,
    nutrient_id: str,
    decision: DecisionItem,
    workspace_root: Path,
    created_at_ms: int | None = None,
) -> ApplyJob:
    now_ms = created_at_ms if created_at_ms is not None else _now_ms()
    return ApplyJob(
        job_id=_job_id(review_id=review_id, nutrient_id=nutrient_id),
        review_id=review_id,
        session_id=session_id,
        run_id=run_id,
        nutrient_id=nutrient_id,
        decision=decision.decision,
        target=decision.target,
        workspace_root=str(workspace_root.resolve()),
        status="pending",
        attempt_count=0,
        created_at_ms=now_ms,
        updated_at_ms=now_ms,
    )


async def execute_apply_job(
    *,
    cfg: Config,
    store: EvolutionStore,
    job: ApplyJob,
    nutrient: EvolutionNutrient,
) -> ApplyExecutionResult:
    running = await store.write_apply_job(job.mark_running(updated_at_ms=_now_ms()))
    workspace_root = Path(running.workspace_root).expanduser().resolve()
    if running.decision == "ignore":
        decision_record = await store.record_apply_result(
            review_id=running.review_id,
            nutrient_id=running.nutrient_id,
            applied_status="skipped",
            applied_mode="ignore",
            applied_at_ms=_now_ms(),
        )
        finished = await store.write_apply_job(
            running.mark_finished(
                artifact_path=None,
                mode="ignore",
                updated_at_ms=_now_ms(),
            )
        )
        return ApplyExecutionResult(job=finished, decision_record=decision_record)
    if running.decision == "accept_memory":
        try:
            memory_store = MemoryStore(memory_dir=_resolve_memory_dir(cfg, workspace_root))
            outcome = await MemoryMaterializer(
                memory_store,
                run_id=f"decision-apply:{running.run_id}:{running.nutrient_id}",
            ).materialize(nutrient)
            decision_record = await store.record_apply_result(
                review_id=running.review_id,
                nutrient_id=running.nutrient_id,
                applied_status=cast(DecisionApplyStatus, outcome.status),
                applied_path=outcome.path,
                applied_mode="append",
                applied_at_ms=outcome.applied_at_ms,
                applied_error=outcome.error,
            )
            finished = await store.write_apply_job(
                running.mark_finished(
                    artifact_path=outcome.path,
                    mode="append",
                    updated_at_ms=outcome.applied_at_ms,
                )
            )
            return ApplyExecutionResult(job=finished, decision_record=decision_record)
        except Exception as exc:
            error = str(exc)
            decision_record = await store.record_apply_result(
                review_id=running.review_id,
                nutrient_id=running.nutrient_id,
                applied_status="failed",
                applied_mode="append",
                applied_at_ms=_now_ms(),
                applied_error=error,
            )
            failed = await store.write_apply_job(
                running.mark_failed(error=error, updated_at_ms=_now_ms())
            )
            return ApplyExecutionResult(job=failed, decision_record=decision_record)
    try:
        result = await materialize_skill(workspace_root, nutrient)
        applied_at_ms = _now_ms()
        decision_record = await store.record_apply_result(
            review_id=running.review_id,
            nutrient_id=running.nutrient_id,
            applied_status=result.status,
            applied_path=result.path,
            applied_mode=result.mode,
            applied_at_ms=applied_at_ms,
        )
        finished = await store.write_apply_job(
            running.mark_finished(
                artifact_path=result.path,
                mode=result.mode,
                updated_at_ms=applied_at_ms,
            )
        )
        return ApplyExecutionResult(job=finished, decision_record=decision_record)
    except Exception as exc:
        error = str(exc)
        decision_record = await store.record_apply_result(
            review_id=running.review_id,
            nutrient_id=running.nutrient_id,
            applied_status="failed",
            applied_at_ms=_now_ms(),
            applied_error=error,
        )
        failed = await store.write_apply_job(
            running.mark_failed(error=error, updated_at_ms=_now_ms())
        )
        return ApplyExecutionResult(job=failed, decision_record=decision_record)


async def recover_pending_apply_jobs(cfg: Config) -> tuple[ApplyJob, ...]:
    root_dir = resolve_evolution_root(cfg.evolution.learning.root_path)
    store = EvolutionStore(root_dir=root_dir, state_store=EvolutionStateStore(root_dir))
    jobs = await store.list_recoverable_apply_jobs()
    recovered: list[ApplyJob] = []
    for job in jobs:
        decision_record = await store.read_decision(job.review_id)
        if decision_record is not None:
            item = next(
                (entry for entry in decision_record.items if entry.nutrient_id == job.nutrient_id),
                None,
            )
            if item is not None and item.applied_status in {"written", "skipped"}:
                finished = await store.write_apply_job(
                    job.mark_finished(
                        artifact_path=item.applied_path,
                        mode=item.applied_mode,
                        updated_at_ms=item.applied_at_ms or _now_ms(),
                    )
                )
                recovered.append(finished)
                continue
        review = await store.read_review(job.run_id)
        if review is None:
            failed = await store.write_apply_job(
                job.mark_failed(error=f"review not found: {job.run_id}", updated_at_ms=_now_ms())
            )
            recovered.append(failed)
            continue
        nutrient = next(
            (entry for entry in review.nutrients if entry.nutrient_id == job.nutrient_id), None
        )
        if nutrient is None:
            failed = await store.write_apply_job(
                job.mark_failed(
                    error=f"nutrient not found: {job.nutrient_id}",
                    updated_at_ms=_now_ms(),
                )
            )
            recovered.append(failed)
            continue
        outcome = await execute_apply_job(cfg=cfg, store=store, job=job, nutrient=nutrient)
        recovered.append(outcome.job)
    return tuple(recovered)


def _job_id(*, review_id: str, nutrient_id: str) -> str:
    return f"apply:{review_id}:{nutrient_id}"


def _resolve_memory_dir(cfg: Config, workspace_root: Path) -> Path:
    raw = str(cfg.evolution.memory.root_path).strip()
    expanded = Path(raw).expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    return (workspace_root / expanded).resolve()


def _now_ms() -> int:
    return int(time.time() * 1000)
