"""SessionTaskProgressManager 单元测试。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from infrastructure.config.models import Config
from sessions import TASK_PROGRESS_MAX_ITEMS, SessionTaskProgressManager


def _cfg(session_root: Path) -> Config:
    return Config.model_validate(
        {
            "model": {
                "name": "fake",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key": "",
            },
            "session": {
                "backend": "file",
                "file_store_path": str(session_root),
            },
        }
    )


def _task(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "manual:run-1",
        "orchestration_task_id": "manual:run-1",
        "task_id": "task-1",
        "task_run_id": "run-1",
        "desc": "实现后端进度模型",
        "status": "in_progress",
        "display_order": 0,
    }
    payload.update(overrides)
    return payload


def test_read_missing_file_returns_empty_snapshot(tmp_path: Path) -> None:
    manager = SessionTaskProgressManager.from_config(_cfg(tmp_path / "sessions"))

    snapshot = manager.read_snapshot("thread-abc123abc123")

    assert snapshot.schema_version == 1
    assert snapshot.session_id == "thread-abc123abc123"
    assert snapshot.source == "api"
    assert snapshot.tasks == []
    assert snapshot.counts.model_dump() == {
        "pending": 0,
        "in_progress": 0,
        "completed": 0,
        "total": 0,
    }


def test_write_snapshot_computes_counts_and_uses_configured_path(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    manager = SessionTaskProgressManager.from_config(_cfg(root))

    snapshot = manager.write_snapshot(
        "thread-abc123abc123",
        [
            _task(status="completed", display_order=1),
            _task(
                id="manual:run-2",
                orchestration_task_id="manual:run-2",
                task_id="task-2",
                task_run_id="run-2",
                status="pending",
                display_order=0,
            ),
        ],
        source="api",
    )

    path = root / "thread-abc123abc123" / "task_progress.json"
    assert path.exists()
    assert snapshot.counts.model_dump() == {
        "pending": 1,
        "in_progress": 0,
        "completed": 1,
        "total": 2,
    }
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["source"] == "api"
    assert [item["display_order"] for item in data["tasks"]] == [0, 1]


def test_read_snapshot_recomputes_missing_counts(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    session_id = "thread-abc123abc123"
    path = root / session_id / "task_progress.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": session_id,
                "updated_at_ms": 1,
                "source": "workflow",
                "tasks": [
                    _task(status="completed", display_order=1),
                    _task(
                        id="manual:run-2",
                        orchestration_task_id="manual:run-2",
                        task_id="task-2",
                        task_run_id="run-2",
                        status="pending",
                        display_order=0,
                    ),
                ],
            }
        ),
        encoding="utf-8",
    )
    manager = SessionTaskProgressManager.from_config(_cfg(root))

    snapshot = manager.read_snapshot(session_id)

    assert snapshot.counts.model_dump() == {
        "pending": 1,
        "in_progress": 0,
        "completed": 1,
        "total": 2,
    }
    assert [item.display_order for item in snapshot.tasks] == [0, 1]


def test_sync_workflow_tasks_maps_statuses_with_stable_ids(tmp_path: Path) -> None:
    manager = SessionTaskProgressManager.from_config(_cfg(tmp_path / "sessions"))

    snapshot = manager.sync_workflow_tasks(
        "thread-abc123abc123",
        "wf-1",
        [
            {
                "task_id": "assigned",
                "task_run_id": "001-assigned",
                "desc": "已分配任务",
                "status": "assigned",
                "display_order": 0,
            },
            {
                "task_id": "running",
                "task_run_id": "002-running",
                "desc": "运行中任务",
                "status": "running",
                "display_order": 1,
            },
            {
                "task_id": "completed",
                "task_run_id": "003-completed",
                "desc": "已完成任务",
                "status": "completed",
                "display_order": 2,
            },
            {
                "task_id": "failed",
                "task_run_id": "004-failed",
                "desc": "失败任务",
                "status": "failed",
                "display_order": 3,
                "error_message": "child exploded",
            },
        ],
    )

    assert snapshot.source == "workflow"
    assert snapshot.counts.model_dump() == {
        "pending": 2,
        "in_progress": 1,
        "completed": 1,
        "total": 4,
    }
    assert [
        (
            item.orchestration_task_id,
            item.task_id,
            item.task_run_id,
            item.desc,
            item.status,
            item.source_status,
            item.error_message,
        )
        for item in snapshot.tasks
    ] == [
        (
            "wf-1:001-assigned",
            "assigned",
            "001-assigned",
            "已分配任务",
            "pending",
            "assigned",
            None,
        ),
        (
            "wf-1:002-running",
            "running",
            "002-running",
            "运行中任务",
            "in_progress",
            "running",
            None,
        ),
        (
            "wf-1:003-completed",
            "completed",
            "003-completed",
            "已完成任务",
            "completed",
            "completed",
            None,
        ),
        (
            "wf-1:004-failed",
            "failed",
            "004-failed",
            "失败任务",
            "pending",
            "failed",
            "child exploded",
        ),
    ]


def test_write_snapshot_rejects_invalid_required_fields(tmp_path: Path) -> None:
    manager = SessionTaskProgressManager.from_config(_cfg(tmp_path / "sessions"))

    with pytest.raises(ValueError, match="status"):
        manager.write_snapshot(
            "thread-abc123abc123",
            [_task(status="failed")],
            source="api",
        )

    with pytest.raises(ValueError, match="field must be a non-empty string"):
        manager.write_snapshot(
            "thread-abc123abc123",
            [_task(desc="")],
            source="api",
        )


def test_write_uses_atomic_replace_path(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    manager = SessionTaskProgressManager.from_config(_cfg(root))

    manager.write_snapshot("thread-abc123abc123", [_task()], source="llm")

    session_dir = root / "thread-abc123abc123"
    assert (session_dir / "task_progress.json").is_file()
    assert not (session_dir / ".task_progress.json.tmp").exists()


def test_write_uses_lock_and_unique_tmp_for_concurrent_writers(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    manager = SessionTaskProgressManager.from_config(_cfg(root))
    session_id = "thread-abc123abc123"

    def write(index: int) -> None:
        manager.write_snapshot(
            session_id,
            [
                _task(
                    id=f"manual:run-{index}",
                    orchestration_task_id=f"manual:run-{index}",
                    task_id=f"task-{index}",
                    task_run_id=f"run-{index}",
                    desc=f"任务 {index}",
                    display_order=index,
                )
            ],
            source="api",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(16)))

    session_dir = root / session_id
    snapshot = manager.read_snapshot(session_id)
    assert snapshot.counts.total == 1
    assert (session_dir / "task_progress.json").is_file()
    assert not list(session_dir.glob(".task_progress.json.*.tmp"))
    assert not (session_dir / ".task_progress.json.lock").exists()


def test_write_snapshot_rejects_too_many_tasks(tmp_path: Path) -> None:
    manager = SessionTaskProgressManager.from_config(_cfg(tmp_path / "sessions"))
    tasks = [
        _task(
            id=f"manual:run-{index}",
            orchestration_task_id=f"manual:run-{index}",
            task_id=f"task-{index}",
            task_run_id=f"run-{index}",
            display_order=index,
        )
        for index in range(TASK_PROGRESS_MAX_ITEMS + 1)
    ]

    with pytest.raises(ValueError, match="at most 128 items"):
        manager.write_snapshot("thread-abc123abc123", tasks, source="api")


def test_rejects_session_path_traversal(tmp_path: Path) -> None:
    manager = SessionTaskProgressManager.from_config(_cfg(tmp_path / "sessions"))

    with pytest.raises(ValueError, match="single path segment"):
        manager.read_snapshot("../outside")
