"""SessionTaskProgressManager v2 仓储边界测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from infrastructure.config.models import Config
from sessions import (
    TASK_PROGRESS_MAX_ITEMS,
    SessionTaskProgressManager,
    TaskProgressControlMode,
    TaskProgressTaskDefinition,
)


def _cfg(session_root: Path) -> Config:
    """构造文件 session 配置，输入为临时根路径，输出为可用 Config。"""
    return Config.model_validate(
        {
            "model": {"preset_id": "local-gemma-4-e4b-it"},
            "session": {"backend": "file", "file_store_path": str(session_root)},
        }
    )


def _definition(index: int) -> TaskProgressTaskDefinition:
    """构造固定任务骨架，输入为顺序号，输出为 v2 不可变定义。"""
    return TaskProgressTaskDefinition(
        task_id=f"task-{index}",
        task_run_id=f"{index:03d}-task-{index}",
        desc=f"任务 {index}",
        display_order=index,
    )


def test_read_missing_file_returns_v2_empty_snapshot(tmp_path: Path) -> None:
    """缺失文件返回无 foreground 坐标的 v2 空快照。"""
    manager = SessionTaskProgressManager.from_config(_cfg(tmp_path / "sessions"))

    snapshot = manager.read_snapshot("thread-abc123abc123")

    assert snapshot.schema_version == 2
    assert snapshot.workflow_id is None
    assert snapshot.title is None
    assert snapshot.control_mode is None
    assert snapshot.tasks == []
    assert snapshot.counts.model_dump() == {
        "pending": 0,
        "in_progress": 0,
        "completed": 0,
        "failed": 0,
        "cancelled": 0,
        "total": 0,
    }


def test_open_workflow_persists_sorted_v2_snapshot_atomically(tmp_path: Path) -> None:
    """初始化 workflow 写入 v2 快照，并清理原子写入中间文件。"""
    root = tmp_path / "sessions"
    manager = SessionTaskProgressManager.from_config(_cfg(root))

    snapshot = manager.open_workflow(
        session_id="thread-abc123abc123",
        workflow_id="wf-1",
        title="构建服务",
        control_mode=TaskProgressControlMode.RUNTIME_LIFECYCLE,
        tasks=[_definition(1), _definition(0)],
    )

    session_dir = root / "thread-abc123abc123"
    path = session_dir / "task_progress.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert [task.display_order for task in snapshot.tasks] == [0, 1]
    assert payload["schema_version"] == 2
    assert payload["workflow_id"] == "wf-1"
    assert [task["display_order"] for task in payload["tasks"]] == [0, 1]
    assert not list(session_dir.glob(".task_progress.json.*.tmp"))
    assert not (session_dir / ".task_progress.json.lock").exists()


def test_v1_snapshot_is_rejected_instead_of_becoming_current_truth(tmp_path: Path) -> None:
    """遗留 v1 数据拒绝读取，避免旧多 owner 字段重新参与当前状态。"""
    root = tmp_path / "sessions"
    session_id = "thread-abc123abc123"
    path = root / session_id / "task_progress.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"schema_version": 1, "session_id": session_id, "tasks": []}),
        encoding="utf-8",
    )
    manager = SessionTaskProgressManager.from_config(_cfg(root))

    with pytest.raises(ValidationError, match="schema_version"):
        manager.read_snapshot(session_id)


@pytest.mark.parametrize("session_id", ["", ".", "..", "../thread", "a/b", "a\\b"])
def test_rejects_non_segment_session_id(tmp_path: Path, session_id: str) -> None:
    """session 路径只能接受单路径段，阻止仓储路径逃逸。"""
    manager = SessionTaskProgressManager.from_config(_cfg(tmp_path / "sessions"))

    with pytest.raises(ValueError, match="session_id"):
        manager.read_snapshot(session_id)


def test_open_workflow_rejects_more_than_maximum_tasks(tmp_path: Path) -> None:
    """单 workflow 的任务数量受 v2 上限约束。"""
    manager = SessionTaskProgressManager.from_config(_cfg(tmp_path / "sessions"))
    definitions = [_definition(index) for index in range(TASK_PROGRESS_MAX_ITEMS + 1)]

    with pytest.raises(ValueError, match="at most"):
        manager.open_workflow(
            session_id="thread-abc123abc123",
            workflow_id="wf-1",
            title="过大任务",
            control_mode=TaskProgressControlMode.RUNTIME_LIFECYCLE,
            tasks=definitions,
        )
