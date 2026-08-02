"""scheduled run 统一所有权合同测试。

覆盖 reservation identity、同任务多 live run 的精确终态 CAS，以及重启时
逐 run 收口。测试使用真实 Store，保证 append-only run 日志与内存投影一致。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path

import pytest

from core.contracts import RunExecutionOverrides
from scheduler.domain import (
    ConcurrencyPolicy,
    DueTaskReservation,
    RunStatus,
    ScheduledRun,
    ScheduledTask,
    ScheduleTrigger,
    TaskExecutionPolicy,
    TaskLifecycleState,
    TaskOrigin,
    TaskTarget,
    TriggerType,
)
from scheduler.store import Store


def test_run_execution_overrides_wraps_runtime_safety_without_raw_replacement() -> None:
    """scheduled override 只能装饰 SessionEngine approval，不能替换安全门户。"""
    override_fields = {field.name for field in fields(RunExecutionOverrides)}
    assert "approval" not in override_fields
    assert "approval_transform" in override_fields
    snapshot = RunExecutionOverrides(run_id="run-contract")
    with pytest.raises(FrozenInstanceError):
        snapshot.run_id = "mutated"  # type: ignore[misc]


def _make_task(task_id: str = "task-contract") -> ScheduledTask:
    """构造允许并发的最小定时任务，输出可直接写入真实 Store。"""
    timestamp = "2026-07-31T00:00:00+00:00"
    return ScheduledTask(
        task_id=task_id,
        name="ownership contract",
        lifecycle=TaskLifecycleState.SCHEDULED,
        origin=TaskOrigin.SYSTEM,
        trigger=ScheduleTrigger(
            trigger_type=TriggerType.CRON,
            expr="* * * * *",
            timezone="UTC",
        ),
        policy=TaskExecutionPolicy(concurrency_policy=ConcurrencyPolicy.ALLOW),
        target=TaskTarget(agent_name="default", input_text="run"),
        next_run_at=timestamp,
        last_run_at=None,
        created_by="test",
        created_at=timestamp,
        updated_at=timestamp,
        thread_id="thread-aaaaaaaaaaaa",
    )


def _running_run(
    task: ScheduledTask,
    *,
    run_id: str,
    reservation_id: str,
) -> ScheduledRun:
    """构造带 reservation identity 的 RUNNING 记录。"""
    return ScheduledRun(
        run_id=run_id,
        task_id=task.task_id,
        status=RunStatus.RUNNING,
        scheduled_for=task.next_run_at or task.created_at,
        started_at=task.created_at,
        finished_at=None,
        session_id=f"session-{run_id}",
        result_status=None,
        final_message_excerpt=None,
        error_message=None,
        failure_reason=None,
        delivery_error=None,
        silent_suppressed=False,
        thread_id=task.thread_id,
        reservation_id=reservation_id,
    )


def test_due_reservation_has_stable_non_empty_identity() -> None:
    """同一个 reservation 对象重试时保持同一主键。"""
    task = _make_task()
    reservation = DueTaskReservation(
        task=task,
        scheduled_for=task.next_run_at or task.created_at,
        reserved_at=task.created_at,
    )

    assert reservation.reservation_id
    assert replace(reservation).reservation_id == reservation.reservation_id


def test_finish_run_if_running_targets_exact_run_and_first_terminal_wins(
    tmp_path: Path,
) -> None:
    """ALLOW 下旧 run 即使已经不是 latest，也能按 run_id 独立终态化。"""
    store = Store(tmp_path)
    task = store.create_task(_make_task())
    run_a = _running_run(task, run_id="run-a", reservation_id="reservation-a")
    run_b = _running_run(task, run_id="run-b", reservation_id="reservation-b")
    store.append_run(run_a)
    store.append_run(run_b)

    completed_a = replace(
        run_a,
        status=RunStatus.COMPLETED,
        finished_at="2026-07-31T00:00:10+00:00",
        result_status="completed",
    )
    late_failed_a = replace(
        completed_a,
        status=RunStatus.FAILED,
        result_status="failed",
        error_message="late failure",
    )

    assert store.finish_run_if_running(completed_a) is True
    assert store.finish_run_if_running(late_failed_a) is False
    persisted_a = store.get_run(task.task_id, "run-a")
    persisted_b = store.get_run(task.task_id, "run-b")
    assert persisted_a is not None
    assert persisted_a.status is RunStatus.COMPLETED
    assert persisted_b is not None
    assert persisted_b.status is RunStatus.RUNNING


def test_recover_stale_runs_closes_every_running_allow_run(tmp_path: Path) -> None:
    """重启恢复遍历全部 live run，避免只收 latest 留下幽灵 RUNNING。"""
    store = Store(tmp_path)
    task = store.create_task(_make_task())
    store.append_run(_running_run(task, run_id="run-a", reservation_id="reservation-a"))
    store.append_run(_running_run(task, run_id="run-b", reservation_id="reservation-b"))

    assert store.recover_stale_runs() == 2
    assert {run.status for run in store.list_runs(task.task_id, limit=None)} == {
        RunStatus.ABANDONED
    }


def test_cancelled_terminal_rejects_late_completed_write(tmp_path: Path) -> None:
    """replace 先抢占 CANCELLED 后，旧执行体的迟到成功无法覆盖终态。"""
    store = Store(tmp_path)
    task = store.create_task(_make_task())
    running = _running_run(
        task,
        run_id="run-replaced",
        reservation_id="reservation-replaced",
    )
    store.append_run(running)

    store.cancel_run(
        run_id=running.run_id,
        task_id=task.task_id,
        error_message="replaced_by_new_run",
        cancel_reason="replaced_by_new_run",
    )
    late_completed = replace(
        running,
        status=RunStatus.COMPLETED,
        finished_at="2026-07-31T00:00:10+00:00",
        result_status="completed",
    )

    assert store.finish_run_if_running(late_completed) is False
    winner = store.get_run(task.task_id, running.run_id)
    assert winner is not None
    assert winner.status is RunStatus.CANCELLED
    assert winner.cancel_reason == "replaced_by_new_run"
