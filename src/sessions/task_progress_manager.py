"""Session 任务进度公共 Manager。

本脚本提供 Web、LLM 工具和 workflow bridge 共享的任务级入口。
关键流程：读取当前 session 快照，写入外部任务列表，同步 workflow 任务状态，并统一计算 counts。
关键函数：read_snapshot 读取快照，write_snapshot 写入 API/LLM 快照，sync_workflow_tasks 写入编排快照。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from infrastructure.config.models import Config
from sessions._task_progress_repository import SessionTaskProgressRepository
from sessions.task_progress_models import (
    TaskProgressItem,
    TaskProgressSnapshot,
    TaskProgressSource,
    WorkflowTaskProgressInput,
    compute_counts,
    current_time_ms,
)

_WORKFLOW_STATUS_MAP: dict[str, str] = {
    "assigned": "pending",
    "running": "in_progress",
    "completed": "completed",
    "failed": "pending",
}


class SessionTaskProgressManager:
    """Session 任务进度公共入口。"""

    def __init__(self, repository: SessionTaskProgressRepository) -> None:
        self._repository = repository

    @classmethod
    def from_config(cls, config: Config) -> SessionTaskProgressManager:
        """从 Config 构建 Manager。"""
        return cls(SessionTaskProgressRepository.from_config(config))

    @property
    def repository(self) -> SessionTaskProgressRepository:
        """暴露 repository 给测试检查路径。"""
        return self._repository

    def read_snapshot(self, session_id: str) -> TaskProgressSnapshot:
        """读取当前 session 快照；缺失文件返回空快照。"""
        return self._repository.read(session_id)

    def write_snapshot(
        self,
        session_id: str,
        tasks: Sequence[TaskProgressItem | dict[str, Any]],
        source: TaskProgressSource,
    ) -> TaskProgressSnapshot:
        """校验任务列表、计算 counts，并写入当前 session。"""
        now = current_time_ms()
        normalized = [self._normalize_task(item, now=now) for item in tasks]
        snapshot = TaskProgressSnapshot(
            schema_version=1,
            session_id=session_id,
            updated_at_ms=now,
            source=source,
            tasks=normalized,
            counts=compute_counts(normalized),
        )
        return self._repository.write(session_id, snapshot, expected_source=source)

    def sync_workflow_tasks(
        self,
        session_id: str,
        workflow_id: str,
        tasks: Sequence[WorkflowTaskProgressInput | dict[str, Any]],
    ) -> TaskProgressSnapshot:
        """把 workflow 任务状态同步为 session 任务进度快照。"""
        if not isinstance(workflow_id, str) or not workflow_id.strip():
            raise ValueError("workflow_id must be a non-empty string")

        now = current_time_ms()
        items: list[TaskProgressItem] = []
        for raw in tasks:
            task = (
                raw
                if isinstance(raw, WorkflowTaskProgressInput)
                else WorkflowTaskProgressInput.model_validate(raw)
            )
            status = _WORKFLOW_STATUS_MAP[task.status]
            orchestration_task_id = f"{workflow_id.strip()}:{task.task_run_id}"
            items.append(
                TaskProgressItem(
                    id=orchestration_task_id,
                    orchestration_task_id=orchestration_task_id,
                    workflow_id=workflow_id.strip(),
                    task_id=task.task_id,
                    task_run_id=task.task_run_id,
                    desc=task.desc,
                    status=status,  # type: ignore[arg-type]
                    source_status=task.status,
                    error_message=task.error_message,
                    display_order=task.display_order,
                    updated_at_ms=now,
                )
            )
        return self.write_snapshot(session_id, items, source="workflow")

    def _normalize_task(
        self, item: TaskProgressItem | dict[str, Any], *, now: int
    ) -> TaskProgressItem:
        """校验单条任务并补齐 item 更新时间。"""
        task = item if isinstance(item, TaskProgressItem) else TaskProgressItem.model_validate(item)
        if task.updated_at_ms is None:
            task = task.model_copy(update={"updated_at_ms": now})
        return task


__all__ = ["SessionTaskProgressManager"]
