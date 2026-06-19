"""LLM 任务进度内置工具。

本脚本提供 update_task_progress 工具，让模型只写当前 ToolContext.session_id 绑定的 session。
关键流程：校验参数字段，按 workflow_id + step_id 派生稳定 progress key，调用 SessionTaskProgressManager 写入 source=llm 快照。
关键函数：TaskProgressTool._run 执行工具写入，build_task_progress_tool_from_config 按 Config 装配工具。
"""

from __future__ import annotations

from typing import Any

from core.contracts import ToolContext
from infrastructure.config.models import Config
from sessions import (
    TASK_PROGRESS_MAX_DESC_LENGTH,
    TASK_PROGRESS_MAX_ERROR_LENGTH,
    TASK_PROGRESS_MAX_ID_LENGTH,
    TASK_PROGRESS_MAX_ITEMS,
    SessionTaskProgressManager,
    TaskProgressItem,
)
from tools.runtime.base import BaseBuiltinTool

_TOP_LEVEL_KEYS = frozenset({"tasks"})
_TASK_KEYS = frozenset(
    {
        "step_id",
        "task_id",
        "task_run_id",
        "desc",
        "status",
        "display_order",
        "orchestration_task_id",
        "workflow_id",
        "source_status",
        "error_message",
    }
)
_WORKFLOW_KEY_PREFIX = "wf-"


class TaskProgressTool(BaseBuiltinTool):
    """把任务进度写入当前 ToolContext session。"""

    name = "update_task_progress"
    description = (
        "更新当前会话的任务进度快照。"
        "只能写入当前 ToolContext.session_id 绑定的 session；参数不能指定 session_id。"
        "推荐每个 task 只填写 workflow_id、step_id、desc、status；"
        "后端会派生 id、orchestration_task_id 和 task_run_id。"
        "旧字段 task_id、task_run_id、orchestration_task_id 仅作为兼容输入；"
        "status 默认 pending，display_order 默认按数组顺序。"
    )
    input_schema: dict[str, Any] = {  # noqa: RUF012
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "description": "当前 session 的任务进度列表。",
                "maxItems": TASK_PROGRESS_MAX_ITEMS,
                "items": {
                    "type": "object",
                    "properties": {
                        "step_id": {
                            "type": "string",
                            "description": "计划步骤 ID；task_flow 中对应 plan node id。",
                            "maxLength": TASK_PROGRESS_MAX_ID_LENGTH,
                        },
                        "task_id": {"type": "string", "maxLength": TASK_PROGRESS_MAX_ID_LENGTH},
                        "task_run_id": {
                            "type": "string",
                            "maxLength": TASK_PROGRESS_MAX_ID_LENGTH,
                        },
                        "desc": {"type": "string", "maxLength": TASK_PROGRESS_MAX_DESC_LENGTH},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                            "default": "pending",
                        },
                        "display_order": {"type": "integer", "minimum": 0},
                        "orchestration_task_id": {
                            "type": "string",
                            "maxLength": TASK_PROGRESS_MAX_ID_LENGTH,
                        },
                        "workflow_id": {
                            "type": "string",
                            "maxLength": TASK_PROGRESS_MAX_ID_LENGTH,
                        },
                        "source_status": {
                            "type": "string",
                            "maxLength": TASK_PROGRESS_MAX_ID_LENGTH,
                        },
                        "error_message": {
                            "type": "string",
                            "maxLength": TASK_PROGRESS_MAX_ERROR_LENGTH,
                        },
                    },
                    "required": ["desc"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["tasks"],
        "additionalProperties": False,
    }

    def __init__(self, manager: SessionTaskProgressManager) -> None:
        super().__init__()
        self._manager = manager

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        if "session_id" in args:
            raise ValueError("session_id is not accepted; current ToolContext.session_id is used")
        unknown_top_keys = sorted(set(args) - _TOP_LEVEL_KEYS)
        if unknown_top_keys:
            raise ValueError(f"unknown top-level fields: {unknown_top_keys}")
        if not ctx.session_id or not ctx.session_id.strip():
            raise ValueError("ToolContext.session_id is required")

        raw_tasks = args["tasks"]
        if not isinstance(raw_tasks, list):
            raise ValueError("tasks must be a list")

        tasks: list[TaskProgressItem] = []
        seen_progress_keys: set[str] = set()
        for index, raw in enumerate(raw_tasks):
            if not isinstance(raw, dict):
                raise ValueError(f"tasks[{index}] must be an object")
            if "session_id" in raw:
                raise ValueError("task item session_id is not accepted")
            unknown_task_keys = sorted(set(raw) - _TASK_KEYS)
            if unknown_task_keys:
                raise ValueError(f"tasks[{index}] has unknown fields: {unknown_task_keys}")
            step_id = self._required_step_id(raw, index)
            task_id = step_id
            workflow_id = self._optional_str(raw, "workflow_id")
            legacy_orchestration_task_id = self._optional_str(raw, "orchestration_task_id")
            orchestration_task_id = self._derive_progress_key(
                workflow_id=workflow_id,
                step_id=step_id,
                legacy_orchestration_task_id=legacy_orchestration_task_id,
                explicit_step_id="step_id" in raw,
            )
            if orchestration_task_id in seen_progress_keys:
                raise ValueError(
                    f"tasks[{index}] derives duplicate progress key: {orchestration_task_id}"
                )
            seen_progress_keys.add(orchestration_task_id)
            task_run_id = self._optional_str(raw, "task_run_id") or step_id
            desc = self._required_str(raw, "desc", index)
            display_order = raw.get("display_order", index)
            task_payload = {
                "id": orchestration_task_id,
                "orchestration_task_id": orchestration_task_id,
                "task_id": task_id,
                "task_run_id": task_run_id,
                "desc": desc,
                "status": raw.get("status", "pending"),
                "display_order": display_order,
                "workflow_id": workflow_id,
                "source_status": self._optional_str(raw, "source_status"),
                "error_message": self._optional_str(raw, "error_message"),
            }
            tasks.append(TaskProgressItem.model_validate(task_payload))

        snapshot = self._manager.write_snapshot(ctx.session_id, tasks, source="llm")
        data = {
            "success": True,
            "session_id": snapshot.session_id,
            "source": snapshot.source,
            "updated_at_ms": snapshot.updated_at_ms,
            "counts": snapshot.counts.model_dump(mode="json"),
            "tasks": [task.model_dump(mode="json") for task in snapshot.tasks],
        }
        return (
            f"task progress updated: {snapshot.counts.completed}/{snapshot.counts.total} completed",
            data,
        )

    def _required_str(self, raw: dict[str, Any], key: str, index: int) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"tasks[{index}].{key} must be a non-empty string")
        return value.strip()

    def _required_step_id(self, raw: dict[str, Any], index: int) -> str:
        """读取 step_id；兼容旧 task_id 字段。"""
        value = raw.get("step_id", raw.get("task_id"))
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"tasks[{index}].step_id must be a non-empty string")
        return value.strip()

    def _optional_str(self, raw: dict[str, Any], key: str) -> str | None:
        value = raw.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        stripped = value.strip()
        return stripped or None

    def _derive_progress_key(
        self,
        *,
        workflow_id: str | None,
        step_id: str,
        legacy_orchestration_task_id: str | None,
        explicit_step_id: bool,
    ) -> str:
        """按新合同派生 progress key，并兼容旧 manual key。"""
        if workflow_id:
            return f"{workflow_id}:{step_id}"
        if legacy_orchestration_task_id and legacy_orchestration_task_id.startswith(
            _WORKFLOW_KEY_PREFIX
        ):
            legacy_workflow_id = legacy_orchestration_task_id.split(":", 1)[0]
            return f"{legacy_workflow_id}:{step_id}"
        if explicit_step_id or legacy_orchestration_task_id is None:
            return f"manual:{step_id}"
        return legacy_orchestration_task_id


def build_task_progress_tool(manager: SessionTaskProgressManager) -> TaskProgressTool:
    """构造 task progress tool。"""
    return TaskProgressTool(manager)


def build_task_progress_tool_from_config(config: Config) -> TaskProgressTool:
    """按 Config 构造 task progress tool。"""
    return build_task_progress_tool(SessionTaskProgressManager.from_config(config))


__all__ = [
    "TaskProgressTool",
    "build_task_progress_tool",
    "build_task_progress_tool_from_config",
]
