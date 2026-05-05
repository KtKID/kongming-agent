"""unit：evolution decision store + replay snapshot。"""

from __future__ import annotations

from pathlib import Path

import pytest

from evolution.models import (
    ApplyJob,
    DecisionItem,
    DecisionRecord,
    DecisionSummary,
    EvolutionNutrient,
    ReviewResult,
    ReviewWritePayload,
)
from evolution.state_store import EvolutionStateStore
from evolution.store import EvolutionStore


def _review_result() -> ReviewResult:
    return ReviewResult(
        run_id="run-parent-1",
        session_id="thread-demo",
        reviewed_at_ms=123456,
        review_summary="captured two nutrients",
        nutrients=(
            EvolutionNutrient(
                nutrient_id="nutrient-1",
                kind="workflow",
                title="Workflow One",
                content="first content",
                summary="first summary",
                confidence=0.91,
                evidence_turns=(1,),
                source_run_id="run-parent-1",
                source_session_id="thread-demo",
                suggested_target="skill",
                tags=("workflow",),
            ),
            EvolutionNutrient(
                nutrient_id="nutrient-2",
                kind="memory",
                title="Memory Two",
                content="second content",
                summary="second summary",
                confidence=0.89,
                evidence_turns=(2,),
                source_run_id="run-parent-1",
                source_session_id="thread-demo",
                suggested_target="memory",
                tags=("memory",),
            ),
        ),
        skip_reasons=(),
    )


@pytest.mark.unit
async def test_notice_snapshots_default_to_pending_counts(tmp_path: Path) -> None:
    root_dir = tmp_path / ".kongming" / "evolution"
    store = EvolutionStore(root_dir=root_dir, state_store=EvolutionStateStore(root_dir))
    await store.write_review(
        ReviewWritePayload(
            review_result=_review_result(),
            trigger_reason="cadence",
        )
    )

    snapshots = await store.list_notice_snapshots_for_session("thread-demo")

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.review_id == "evo-review:run-parent-1"
    assert snapshot.message == "发现 2 条进化养料"
    assert snapshot.nutrient_count == 2
    assert snapshot.handled_count == 0
    assert snapshot.pending_count == 2
    assert snapshot.details["pending_count"] == 2


@pytest.mark.unit
async def test_notice_snapshots_reflect_decision_progress(tmp_path: Path) -> None:
    root_dir = tmp_path / ".kongming" / "evolution"
    store = EvolutionStore(root_dir=root_dir, state_store=EvolutionStateStore(root_dir))
    await store.write_review(
        ReviewWritePayload(
            review_result=_review_result(),
            trigger_reason="cadence",
        )
    )
    record = await store.write_decision(
        DecisionRecord(
            review_id="evo-review:run-parent-1",
            session_id="thread-demo",
            run_id="run-parent-1",
            summary=DecisionSummary(
                total=2,
                accepted_memory=0,
                accepted_skill=0,
                ignored=0,
                pending=2,
            ),
            items=(
                DecisionItem(
                    nutrient_id="nutrient-1",
                    decision="accept_skill",
                    target="skill",
                    decided_at_ms=999,
                ),
            ),
        )
    )

    assert record.summary.accepted_skill == 1
    assert record.summary.pending == 1
    snapshots = await store.list_notice_snapshots_for_session("thread-demo")
    snapshot = snapshots[0]
    assert snapshot.message == "已写入 0/2 条进化养料，待写入 1 条"
    assert snapshot.handled_count == 1
    assert snapshot.pending_count == 1
    assert snapshot.accepted_skill_count == 1
    assert snapshot.details["applied_written_count"] == 0
    assert snapshot.details["applied_pending_count"] == 1


@pytest.mark.unit
async def test_write_decision_persists_apply_fields_and_normalizes_ignore(tmp_path: Path) -> None:
    root_dir = tmp_path / ".kongming" / "evolution"
    store = EvolutionStore(root_dir=root_dir, state_store=EvolutionStateStore(root_dir))

    written = await store.write_decision(
        DecisionRecord(
            review_id="evo-review:run-parent-1",
            session_id="thread-demo",
            run_id="run-parent-1",
            summary=DecisionSummary(
                total=2,
                accepted_memory=0,
                accepted_skill=0,
                ignored=0,
                pending=2,
            ),
            items=(
                DecisionItem(
                    nutrient_id="nutrient-1",
                    decision="accept_memory",
                    target="memory",
                    decided_at_ms=1000,
                    applied_status="written",
                    applied_path=str(tmp_path / ".kongming" / "memory" / "MEMORY.md"),
                    applied_mode="append",
                    applied_at_ms=1001,
                ),
                DecisionItem(
                    nutrient_id="nutrient-2",
                    decision="ignore",
                    decided_at_ms=2000,
                ),
            ),
        )
    )

    assert written.items[0].applied_status == "written"
    assert written.items[0].applied_mode == "append"
    assert written.items[1].applied_status == "skipped"
    assert written.items[1].applied_mode == "ignore"
    assert written.items[1].applied_at_ms == 2000

    replayed = await store.read_decision("evo-review:run-parent-1")
    assert replayed is not None
    assert replayed.items[0].applied_path == str(tmp_path / ".kongming" / "memory" / "MEMORY.md")
    assert replayed.items[1].applied_status == "skipped"
    assert replayed.items[1].applied_mode == "ignore"


@pytest.mark.unit
async def test_record_apply_result_updates_existing_decision_item(tmp_path: Path) -> None:
    root_dir = tmp_path / ".kongming" / "evolution"
    store = EvolutionStore(root_dir=root_dir, state_store=EvolutionStateStore(root_dir))
    await store.write_review(
        ReviewWritePayload(
            review_result=_review_result(),
            trigger_reason="cadence",
        )
    )
    await store.write_decision(
        DecisionRecord(
            review_id="evo-review:run-parent-1",
            session_id="thread-demo",
            run_id="run-parent-1",
            summary=DecisionSummary(
                total=2,
                accepted_memory=0,
                accepted_skill=0,
                ignored=0,
                pending=2,
            ),
            items=(
                DecisionItem(
                    nutrient_id="nutrient-2",
                    decision="accept_memory",
                    target="memory",
                    decided_at_ms=999,
                ),
            ),
        )
    )

    updated = await store.record_apply_result(
        review_id="evo-review:run-parent-1",
        nutrient_id="nutrient-2",
        applied_status="written",
        applied_path=str(tmp_path / ".kongming" / "memory" / "MEMORY.md"),
        applied_mode="append",
        applied_at_ms=1234,
    )

    assert updated.items[0].applied_status == "written"
    assert updated.items[0].applied_mode == "append"
    assert updated.items[0].applied_at_ms == 1234

    replayed = await store.read_decision("evo-review:run-parent-1")
    assert replayed is not None
    assert replayed.items[0].applied_status == "written"
    assert replayed.items[0].applied_path == str(tmp_path / ".kongming" / "memory" / "MEMORY.md")


@pytest.mark.unit
async def test_apply_job_roundtrip_and_recoverable_listing(tmp_path: Path) -> None:
    root_dir = tmp_path / ".kongming" / "evolution"
    workspace = (tmp_path / "workspace").resolve()
    store = EvolutionStore(root_dir=root_dir, state_store=EvolutionStateStore(root_dir))

    pending = await store.write_apply_job(
        ApplyJob(
            job_id="apply:evo-review:run-parent-1:nutrient-1",
            review_id="evo-review:run-parent-1",
            session_id="thread-demo",
            run_id="run-parent-1",
            nutrient_id="nutrient-1",
            decision="accept_skill",
            target="skill",
            workspace_root=str(workspace),
            status="pending",
            attempt_count=0,
            created_at_ms=100,
            updated_at_ms=100,
        )
    )
    await store.write_apply_job(
        ApplyJob(
            job_id="apply:evo-review:run-parent-1:nutrient-2",
            review_id="evo-review:run-parent-1",
            session_id="thread-demo",
            run_id="run-parent-1",
            nutrient_id="nutrient-2",
            decision="accept_memory",
            target="memory",
            workspace_root=str(workspace),
            status="finished",
            attempt_count=1,
            artifact_path=str(tmp_path / ".kongming" / "memory" / "MEMORY.md"),
            mode="append",
            created_at_ms=101,
            updated_at_ms=102,
        )
    )

    replayed = await store.read_apply_job(pending.job_id)
    assert replayed is not None
    assert replayed.status == "pending"

    recoverable = await store.list_recoverable_apply_jobs()
    assert [job.job_id for job in recoverable] == [pending.job_id]
