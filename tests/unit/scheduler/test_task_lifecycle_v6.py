"""scheduler v6 task lifecycle 单一真源回归测试。

覆盖 SC_15—SC_17：
- recurring task 的生命周期与最近 run 结果正交；
- one-shot reservation 原子转 exhausted，领取时不伪造 last_run_at；
- v5 tasks 先备份再迁移到 v6，并用 terminal run.finished_at 纠正 last_run_at。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scheduler.domain import (
    SCHEMA_VERSION,
    RunStatus,
    ScheduledRun,
    TaskLifecycleState,
)
from scheduler.manager import SchedulerManager
from scheduler.store import Store


def _v5_task(
    task_id: str,
    *,
    trigger_type: str,
    enabled: bool,
    state: str,
    next_run_at: str | None,
    last_run_at: str | None,
) -> dict[str, Any]:
    """构造一个可被真实 Store 迁移的 v5 task payload。"""
    return {
        "task_id": task_id,
        "name": task_id,
        "enabled": enabled,
        "state": state,
        "origin": "tool",
        "trigger": {
            "trigger_type": trigger_type,
            "expr": ("2026-07-29T00:00:00+00:00" if trigger_type == "once" else "0 9 * * *"),
            "timezone": "UTC",
        },
        "policy": {
            "session_mode": "fresh_session",
            "concurrency_policy": "forbid",
            "misfire_policy": "skip",
            "max_turns": None,
            "inactivity_timeout_seconds": 600,
            "wall_timeout_seconds": None,
            "retry_limit": 0,
            "silent_marker_enabled": True,
            "approval_mode": None,
        },
        "target": {
            "agent_name": "default",
            "input_text": "ping",
            "metadata": {},
        },
        "next_run_at": next_run_at,
        "last_run_at": last_run_at,
        "created_by": "tester",
        "created_at": "2026-07-28T00:00:00+00:00",
        "updated_at": "2026-07-28T00:00:00+00:00",
        "delivery": None,
        "preset_id": "",
        "thread_id": "",
        "manual_run_requested_at": None,
    }


def _write_terminal_run(
    root: Path,
    *,
    task_id: str,
    status: RunStatus,
    finished_at: str,
) -> None:
    """写入一个真实 v5-compatible terminal run JSONL。"""
    runs_dir = root / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    run = ScheduledRun(
        run_id=f"run-{task_id}",
        task_id=task_id,
        status=status,
        scheduled_for="2026-07-29T00:00:00+00:00",
        started_at="2026-07-29T00:00:01+00:00",
        finished_at=finished_at,
        session_id=f"session-{task_id}",
        result_status=status.value,
        final_message_excerpt=None,
        error_message="boom" if status is RunStatus.FAILED else None,
        failure_reason=None,
        delivery_error=None,
        silent_suppressed=False,
    )
    payload = {
        **run.__dict__,
        "status": run.status.value,
        "failure_reason": None,
        "delivery_status": run.delivery_status.value,
        "_superseded": False,
    }
    (runs_dir / f"{task_id}.jsonl").write_text(
        json.dumps(payload, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def test_v5_to_v6_preserves_tasks_and_corrects_lifecycle_and_last_run(
    tmp_path: Path,
) -> None:
    """SC_17：v5 多状态 fixture 按确定矩阵迁移，原文件保留唯一备份。"""
    claimed_at = "2026-07-29T00:00:02+00:00"
    terminal_at = "2026-07-29T00:00:05+00:00"
    tasks = [
        _v5_task(
            "recurring-failed",
            trigger_type="cron",
            enabled=True,
            state="failed",
            next_run_at="2026-07-30T09:00:00+00:00",
            last_run_at=claimed_at,
        ),
        _v5_task(
            "oneshot-claimed",
            trigger_type="once",
            enabled=False,
            state="completed",
            next_run_at=None,
            last_run_at=claimed_at,
        ),
        _v5_task(
            "paused",
            trigger_type="cron",
            enabled=False,
            state="paused",
            next_run_at="2026-07-30T09:00:00+00:00",
            last_run_at=None,
        ),
        _v5_task(
            "deleted",
            trigger_type="cron",
            enabled=False,
            state="deleted",
            next_run_at=None,
            last_run_at=None,
        ),
    ]
    tasks_path = tmp_path / "scheduled_tasks.json"
    tasks_path.write_text(
        json.dumps({"schema_version": 5, "updated_at": claimed_at, "tasks": tasks}),
        encoding="utf-8",
    )
    _write_terminal_run(
        tmp_path,
        task_id="recurring-failed",
        status=RunStatus.FAILED,
        finished_at=terminal_at,
    )

    store = Store(tmp_path)

    assert SCHEMA_VERSION == 6
    migrated = {task.task_id: task for task in store.list_tasks(include_disabled=True)}
    assert migrated["recurring-failed"].lifecycle is TaskLifecycleState.SCHEDULED
    assert migrated["recurring-failed"].last_run_at == terminal_at
    assert migrated["oneshot-claimed"].lifecycle is TaskLifecycleState.EXHAUSTED
    assert migrated["oneshot-claimed"].last_run_at is None
    assert migrated["paused"].lifecycle is TaskLifecycleState.PAUSED
    assert migrated["deleted"].lifecycle is TaskLifecycleState.DELETED

    backups = list(tmp_path.glob("scheduled_tasks.json.v5.bak.*"))
    assert len(backups) == 1
    backup = json.loads(backups[0].read_text(encoding="utf-8"))
    assert backup["schema_version"] == 5
    assert len(backup["tasks"]) == 4

    current = json.loads(tasks_path.read_text(encoding="utf-8"))
    assert current["schema_version"] == 6
    assert all("lifecycle" in task for task in current["tasks"])
    assert all("enabled" not in task and "state" not in task for task in current["tasks"])


def test_one_shot_reservation_atomically_exhausts_without_last_run_at(
    tmp_path: Path,
) -> None:
    """SC_16：同一个 one-shot 只能领取一次，last_run_at 等 terminal run。"""
    tasks_path = tmp_path / "scheduled_tasks.json"
    task = _v5_task(
        "oneshot",
        trigger_type="once",
        enabled=True,
        state="scheduled",
        next_run_at="2026-07-29T00:00:00+00:00",
        last_run_at=None,
    )
    tasks_path.write_text(
        json.dumps(
            {
                "schema_version": 5,
                "updated_at": "2026-07-28T00:00:00+00:00",
                "tasks": [task],
            }
        ),
        encoding="utf-8",
    )
    store = Store(tmp_path)

    first = store.reserve_due_tasks(now="2026-07-29T00:00:01+00:00")
    second = store.reserve_due_tasks(now="2026-07-29T00:00:02+00:00")

    assert len(first) == 1
    assert second == []
    persisted = store.get_task("oneshot")
    assert persisted is not None
    assert persisted.lifecycle is TaskLifecycleState.EXHAUSTED
    assert persisted.next_run_at is None
    assert persisted.last_run_at is None


def test_exhausted_one_shot_can_have_live_running_projection(tmp_path: Path) -> None:
    """SC_26：one-shot lifecycle=exhausted 与当前 live run=running 可同时成立。"""
    tasks_path = tmp_path / "scheduled_tasks.json"
    task = _v5_task(
        "oneshot-live",
        trigger_type="once",
        enabled=True,
        state="scheduled",
        next_run_at="2026-07-29T00:00:00+00:00",
        last_run_at=None,
    )
    tasks_path.write_text(
        json.dumps(
            {
                "schema_version": 5,
                "updated_at": "2026-07-28T00:00:00+00:00",
                "tasks": [task],
            }
        ),
        encoding="utf-8",
    )
    store = Store(tmp_path)
    reservation = store.reserve_due_tasks(now="2026-07-29T00:00:01+00:00")[0]
    store.append_run(
        ScheduledRun(
            run_id="run-terminal",
            task_id=reservation.task.task_id,
            status=RunStatus.COMPLETED,
            scheduled_for="2026-07-28T00:00:00+00:00",
            started_at="2026-07-28T00:00:01+00:00",
            finished_at="2026-07-28T00:00:02+00:00",
            session_id="session-terminal",
            result_status="completed",
            final_message_excerpt="done",
            error_message=None,
            failure_reason=None,
            delivery_error=None,
            silent_suppressed=False,
        )
    )
    store.append_run(
        ScheduledRun(
            run_id="run-live",
            task_id=reservation.task.task_id,
            status=RunStatus.RUNNING,
            scheduled_for=reservation.scheduled_for,
            started_at="2026-07-29T00:00:01+00:00",
            finished_at=None,
            session_id="session-live",
            result_status=None,
            final_message_excerpt=None,
            error_message=None,
            failure_reason=None,
            delivery_error=None,
            silent_suppressed=False,
        )
    )

    persisted = store.get_task("oneshot-live")
    assert persisted is not None
    projection = SchedulerManager(store).project_task(persisted)
    assert projection.task.lifecycle is TaskLifecycleState.EXHAUSTED
    assert projection.latest_run_status is RunStatus.COMPLETED
    assert projection.live_runtime_status.value == "running"


def test_v5_migration_write_failure_keeps_source_and_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SC_17：迁移写入失败时，v5 原文件与唯一备份都保留原始字节。"""
    task = _v5_task(
        "write-failure",
        trigger_type="cron",
        enabled=True,
        state="scheduled",
        next_run_at="2026-07-30T09:00:00+00:00",
        last_run_at=None,
    )
    tasks_path = tmp_path / "scheduled_tasks.json"
    original = json.dumps(
        {
            "schema_version": 5,
            "updated_at": "2026-07-28T00:00:00+00:00",
            "tasks": [task],
        }
    )
    tasks_path.write_text(original, encoding="utf-8")

    def fail_write(self: Store, tasks: list[dict[str, Any]]) -> None:
        """模拟原子写失败；输入仍是完整迁移结果。"""
        del self
        assert len(tasks) == 1
        raise OSError("disk full")

    monkeypatch.setattr(Store, "_write_tasks_payload", fail_write)

    with pytest.raises(OSError, match="disk full"):
        Store(tmp_path)

    assert tasks_path.read_text(encoding="utf-8") == original
    backups = list(tmp_path.glob("scheduled_tasks.json.v5.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original


def test_v5_migration_failure_after_replace_restores_source_and_reuses_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SC_17：replace 后失败也恢复 v5，重复启动复用同一份原字节备份。"""
    task = _v5_task(
        "post-replace-failure",
        trigger_type="cron",
        enabled=True,
        state="scheduled",
        next_run_at="2026-07-30T09:00:00+00:00",
        last_run_at=None,
    )
    tasks_path = tmp_path / "scheduled_tasks.json"
    original = json.dumps(
        {
            "schema_version": 5,
            "updated_at": "2026-07-28T00:00:00+00:00",
            "tasks": [task],
        }
    )
    tasks_path.write_text(original, encoding="utf-8")
    real_write = Store._write_tasks_payload

    def write_then_fail(
        self: Store,
        tasks: list[dict[str, Any]],
    ) -> None:
        """先完成原子替换，再模拟 replace 后的权限同步失败。"""
        real_write(self, tasks)
        raise OSError("post-replace failure")

    monkeypatch.setattr(Store, "_write_tasks_payload", write_then_fail)

    for _ in range(2):
        with pytest.raises(OSError, match="post-replace failure"):
            Store(tmp_path)
        assert tasks_path.read_text(encoding="utf-8") == original
        backups = list(tmp_path.glob("scheduled_tasks.json.v5.bak.*"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == original
