"""Session 任务进度数据模型。

本脚本定义 task_progress.json 的 Pydantic 合同、状态计数和快照序列化。
关键流程：写入入口先构造 TaskProgressItem，再聚合 TaskProgressSnapshot，最后由仓储落盘。
关键函数：current_time_ms 生成更新时间，compute_counts 计算三态计数，snapshot_to_dict 输出 JSON payload。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.clock import now_epoch_ms

TaskProgressStatus = Literal["pending", "in_progress", "completed"]
TaskProgressSource = Literal["api", "llm", "workflow"]
TASK_PROGRESS_MAX_ITEMS = 128
TASK_PROGRESS_MAX_ID_LENGTH = 256
TASK_PROGRESS_MAX_DESC_LENGTH = 1000
TASK_PROGRESS_MAX_ERROR_LENGTH = 2000


def current_time_ms() -> int:
    """返回 Unix 毫秒时间戳。"""
    return now_epoch_ms()


class TaskProgressCounts(BaseModel):
    """任务状态计数。"""

    model_config = ConfigDict(extra="forbid")

    pending: int = Field(ge=0)
    in_progress: int = Field(ge=0)
    completed: int = Field(ge=0)
    total: int = Field(ge=0)


class TaskProgressItem(BaseModel):
    """单个 session 任务进度项。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=TASK_PROGRESS_MAX_ID_LENGTH)
    orchestration_task_id: str = Field(max_length=TASK_PROGRESS_MAX_ID_LENGTH)
    task_id: str = Field(max_length=TASK_PROGRESS_MAX_ID_LENGTH)
    task_run_id: str = Field(max_length=TASK_PROGRESS_MAX_ID_LENGTH)
    desc: str = Field(max_length=TASK_PROGRESS_MAX_DESC_LENGTH)
    status: TaskProgressStatus
    display_order: int = Field(ge=0)
    workflow_id: str | None = Field(default=None, max_length=TASK_PROGRESS_MAX_ID_LENGTH)
    source_status: str | None = Field(default=None, max_length=TASK_PROGRESS_MAX_ID_LENGTH)
    error_message: str | None = Field(default=None, max_length=TASK_PROGRESS_MAX_ERROR_LENGTH)
    updated_at_ms: int | None = Field(default=None, ge=0)

    @field_validator("id", "orchestration_task_id", "task_id", "task_run_id", "desc")
    @classmethod
    def _require_non_empty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("field must be a non-empty string")
        return value.strip()

    @field_validator("workflow_id", "source_status", "error_message")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def _id_matches_orchestration_task_id(self) -> TaskProgressItem:
        if self.id != self.orchestration_task_id:
            raise ValueError("id must equal orchestration_task_id")
        return self


class TaskProgressSnapshot(BaseModel):
    """当前 session 的任务进度快照。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session_id: str
    updated_at_ms: int = Field(ge=0)
    source: TaskProgressSource
    tasks: list[TaskProgressItem] = Field(max_length=TASK_PROGRESS_MAX_ITEMS)
    counts: TaskProgressCounts

    @field_validator("session_id")
    @classmethod
    def _require_session_id(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("session_id must be a non-empty string")
        return value.strip()


class WorkflowTaskProgressInput(BaseModel):
    """workflow 桥接传给 Manager 的任务进度输入。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(max_length=TASK_PROGRESS_MAX_ID_LENGTH)
    task_run_id: str = Field(max_length=TASK_PROGRESS_MAX_ID_LENGTH)
    desc: str = Field(max_length=TASK_PROGRESS_MAX_DESC_LENGTH)
    status: Literal["assigned", "running", "completed", "failed"]
    display_order: int = Field(ge=0)
    error_message: str | None = Field(default=None, max_length=TASK_PROGRESS_MAX_ERROR_LENGTH)

    @field_validator("task_id", "task_run_id", "desc")
    @classmethod
    def _require_non_empty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("field must be a non-empty string")
        return value.strip()


def compute_counts(tasks: list[TaskProgressItem]) -> TaskProgressCounts:
    """按任务列表重新计算三态 counts。"""
    pending = sum(1 for item in tasks if item.status == "pending")
    in_progress = sum(1 for item in tasks if item.status == "in_progress")
    completed = sum(1 for item in tasks if item.status == "completed")
    return TaskProgressCounts(
        pending=pending,
        in_progress=in_progress,
        completed=completed,
        total=len(tasks),
    )


def snapshot_to_dict(snapshot: TaskProgressSnapshot) -> dict[str, Any]:
    """输出 JSON 兼容 dict。"""
    return snapshot.model_dump(mode="json")


__all__ = [
    "TaskProgressCounts",
    "TaskProgressItem",
    "TaskProgressSnapshot",
    "TaskProgressSource",
    "TaskProgressStatus",
    "WorkflowTaskProgressInput",
    "compute_counts",
    "current_time_ms",
    "snapshot_to_dict",
]
