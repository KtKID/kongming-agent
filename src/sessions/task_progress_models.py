"""Session 任务进度 v2 数据模型。

本模块定义当前 foreground workflow 的单一快照合同与状态枚举。
关键流程：workflow 初始化提供不可变任务定义，Manager 生成可变状态项，仓储统一计算计数并落盘。
关键函数：compute_counts 计算五态计数，snapshot_to_dict 输出 JSON 兼容快照。
"""

from __future__ import annotations

from enum import StrEnum
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.clock import now_epoch_ms

TASK_PROGRESS_MAX_ITEMS = 128
TASK_PROGRESS_MAX_ID_LENGTH = 256
TASK_PROGRESS_MAX_DESC_LENGTH = 1000
TASK_PROGRESS_MAX_ERROR_LENGTH = 2000


class TaskProgressStatus(StrEnum):
    """任务进度的公共五态。"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskProgressControlMode(StrEnum):
    """当前 workflow 的状态事实输入模式。"""

    LLM_STEPS = "llm_steps"
    RUNTIME_LIFECYCLE = "runtime_lifecycle"


class TaskProgressAction(StrEnum):
    """LLM 可提交的有限推进命令。"""

    START = "start"
    NEXT = "next"


class RuntimeTaskProgressStatus(StrEnum):
    """workflow runtime 向进度 Manager 报告的生命周期事实。"""

    ASSIGNED = "assigned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def current_time_ms() -> int:
    """返回 Unix 毫秒时间戳。"""
    return now_epoch_ms()


class TaskProgressCounts(BaseModel):
    """任务状态计数。"""

    model_config = ConfigDict(extra="forbid")

    pending: int = Field(ge=0)
    in_progress: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    total: int = Field(ge=0)


class TaskProgressTaskDefinition(BaseModel):
    """workflow 初始化时固定的任务骨架。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(max_length=TASK_PROGRESS_MAX_ID_LENGTH)
    task_run_id: str = Field(max_length=TASK_PROGRESS_MAX_ID_LENGTH)
    desc: str = Field(max_length=TASK_PROGRESS_MAX_DESC_LENGTH)
    depends_on: tuple[str, ...] = ()
    display_order: int = Field(ge=0)

    @field_validator("task_id", "task_run_id", "desc")
    @classmethod
    def _require_non_empty(cls, value: str) -> str:
        """规范化必填文本，输入为字段值，输出为去空白后的非空文本。"""
        if not isinstance(value, str) or not value.strip():
            raise ValueError("field must be a non-empty string")
        return value.strip()

    @field_validator("depends_on")
    @classmethod
    def _normalize_dependencies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """规范化依赖列表，输入为依赖元组，输出为去重后的依赖元组。"""
        normalized: list[str] = []
        for dependency in value:
            if not isinstance(dependency, str) or not dependency.strip():
                raise ValueError("depends_on entries must be non-empty strings")
            key = dependency.strip()
            if key in normalized:
                raise ValueError(f"duplicate dependency: {key}")
            normalized.append(key)
        return tuple(normalized)

    @model_validator(mode="after")
    def _reject_self_dependency(self) -> TaskProgressTaskDefinition:
        """拒绝自依赖，输入为完整定义，输出为已校验定义。"""
        if self.task_id in self.depends_on:
            raise ValueError(f"task cannot depend on itself: {self.task_id}")
        return self


class TaskProgressItem(BaseModel):
    """当前快照中的单个任务状态。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(max_length=TASK_PROGRESS_MAX_ID_LENGTH)
    task_run_id: str = Field(max_length=TASK_PROGRESS_MAX_ID_LENGTH)
    desc: str = Field(max_length=TASK_PROGRESS_MAX_DESC_LENGTH)
    depends_on: tuple[str, ...] = ()
    status: TaskProgressStatus
    display_order: int = Field(ge=0)
    error_message: str | None = Field(default=None, max_length=TASK_PROGRESS_MAX_ERROR_LENGTH)
    updated_at_ms: int = Field(ge=0)

    @field_validator("task_id", "task_run_id", "desc")
    @classmethod
    def _require_non_empty(cls, value: str) -> str:
        """规范化必填文本，输入为字段值，输出为去空白后的非空文本。"""
        if not isinstance(value, str) or not value.strip():
            raise ValueError("field must be a non-empty string")
        return value.strip()

    @field_validator("depends_on")
    @classmethod
    def _normalize_dependencies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """规范化依赖列表，输入为依赖元组，输出为去重后的依赖元组。"""
        return TaskProgressTaskDefinition._normalize_dependencies(value)

    @field_validator("error_message")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        """规范化可选错误文本，输入为错误消息，输出为空或去空白后的文本。"""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class TaskProgressSnapshot(BaseModel):
    """一个 session 当前 foreground workflow 的任务进度快照。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=2, frozen=True)
    session_id: str
    workflow_id: str | None = Field(default=None, max_length=TASK_PROGRESS_MAX_ID_LENGTH)
    title: str | None = Field(default=None, max_length=TASK_PROGRESS_MAX_DESC_LENGTH)
    control_mode: TaskProgressControlMode | None = None
    updated_at_ms: int = Field(ge=0)
    tasks: list[TaskProgressItem] = Field(max_length=TASK_PROGRESS_MAX_ITEMS)
    counts: TaskProgressCounts

    @field_validator("schema_version")
    @classmethod
    def _require_schema_v2(cls, value: int) -> int:
        """限定 schema 版本，输入为版本号，输出为 v2。"""
        if value != 2:
            raise ValueError("task progress schema_version must be 2")
        return value

    @field_validator("session_id")
    @classmethod
    def _require_session_id(cls, value: str) -> str:
        """规范化 session 标识，输入为 session ID，输出为非空单值。"""
        if not isinstance(value, str) or not value.strip():
            raise ValueError("session_id must be a non-empty string")
        return value.strip()

    @field_validator("workflow_id", "title")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        """规范化可选文本，输入为可选字符串，输出为去空白后的值或空。"""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def _validate_foreground_coordinate(self) -> TaskProgressSnapshot:
        """校验空快照与 foreground 快照坐标，输入为完整快照，输出为已校验快照。"""
        coordinate = (self.workflow_id, self.title, self.control_mode)
        if self.tasks and any(value is None for value in coordinate):
            raise ValueError("non-empty task progress requires workflow_id, title and control_mode")
        if not self.tasks and any(value is not None for value in coordinate):
            raise ValueError("empty task progress cannot carry a foreground workflow coordinate")
        return self


def compute_counts(tasks: list[TaskProgressItem]) -> TaskProgressCounts:
    """按任务列表重新计算五态 counts，输入为任务项，输出为聚合计数。"""
    return TaskProgressCounts(
        pending=sum(task.status is TaskProgressStatus.PENDING for task in tasks),
        in_progress=sum(task.status is TaskProgressStatus.IN_PROGRESS for task in tasks),
        completed=sum(task.status is TaskProgressStatus.COMPLETED for task in tasks),
        failed=sum(task.status is TaskProgressStatus.FAILED for task in tasks),
        cancelled=sum(task.status is TaskProgressStatus.CANCELLED for task in tasks),
        total=len(tasks),
    )


def snapshot_to_dict(snapshot: TaskProgressSnapshot) -> dict[str, object]:
    """输出 JSON 兼容 dict，输入为快照，输出为持久化 payload。"""
    return cast(dict[str, object], snapshot.model_dump(mode="json"))


__all__ = [
    "TASK_PROGRESS_MAX_DESC_LENGTH",
    "TASK_PROGRESS_MAX_ERROR_LENGTH",
    "TASK_PROGRESS_MAX_ID_LENGTH",
    "TASK_PROGRESS_MAX_ITEMS",
    "RuntimeTaskProgressStatus",
    "TaskProgressAction",
    "TaskProgressControlMode",
    "TaskProgressCounts",
    "TaskProgressItem",
    "TaskProgressSnapshot",
    "TaskProgressStatus",
    "TaskProgressTaskDefinition",
    "compute_counts",
    "current_time_ms",
    "snapshot_to_dict",
]
