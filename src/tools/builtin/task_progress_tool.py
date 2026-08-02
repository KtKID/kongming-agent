"""LLM 任务进度命令工具。

本模块把模型可见的进度能力收敛为 start 与 next 两条命令。
关键流程：校验命令坐标和字段白名单，冻结命令参数，交给 SessionTaskProgressManager 在同一持久化事务中推进当前 foreground workflow。
关键函数：TaskProgressTool.prepare 校验命令，TaskProgressTool._run 调用状态 Manager，build_task_progress_tool 按依赖装配工具。
"""

from __future__ import annotations

from typing import Any

from core.contracts import PreparedToolCall, ToolContext
from infrastructure.config.models import Config
from sessions import (
    TASK_PROGRESS_MAX_ID_LENGTH,
    SessionTaskProgressManager,
    TaskProgressAction,
)
from tools.runtime.base import BaseBuiltinTool

_TOP_LEVEL_KEYS = frozenset({"action", "workflow_id", "step_id", "next_step_id"})


class TaskProgressTool(BaseBuiltinTool):
    """执行 LLM 可提交的受限任务进度命令。"""

    name = "advance_task_progress"
    description = (
        "推进当前会话前台 task-flow 的进度。"
        "仅支持 action=start 或 action=next；必须提供 workflow_id 和 step_id。"
        "start 启动一个已就绪步骤；next 完成当前步骤并通过 next_step_id 启动下一步骤，"
        "最后一个步骤使用 next 时省略 next_step_id。"
        "工具不接受 session_id、任务状态、描述、顺序、错误或运行 ID。"
    )
    input_schema: dict[str, Any] = {  # noqa: RUF012
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "next"]},
            "workflow_id": {"type": "string", "maxLength": TASK_PROGRESS_MAX_ID_LENGTH},
            "step_id": {"type": "string", "maxLength": TASK_PROGRESS_MAX_ID_LENGTH},
            "next_step_id": {
                "type": "string",
                "maxLength": TASK_PROGRESS_MAX_ID_LENGTH,
            },
        },
        "required": ["action", "workflow_id", "step_id"],
        "additionalProperties": False,
    }

    def __init__(self, manager: SessionTaskProgressManager) -> None:
        """绑定单一进度 Manager，输入为 Manager，输出为空。"""
        super().__init__()
        self._manager = manager

    def prepare(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> PreparedToolCall:
        """校验并冻结命令，输入为模型参数和工具上下文，输出为可执行调用。"""
        self._validate_args(arguments)
        unknown_keys = sorted(set(arguments) - _TOP_LEVEL_KEYS)
        if unknown_keys:
            raise ValueError(f"unknown task progress fields: {unknown_keys}")
        session_id = self._required_string(context.session_id, "ToolContext.session_id")
        action = self._parse_action(arguments.get("action"))
        workflow_id = self._required_string(arguments.get("workflow_id"), "workflow_id")
        step_id = self._required_string(arguments.get("step_id"), "step_id")
        next_step_id = self._optional_string(arguments.get("next_step_id"), "next_step_id")
        if action is TaskProgressAction.START and next_step_id is not None:
            raise ValueError("next_step_id is only accepted for action=next")
        return PreparedToolCall(
            arguments={
                "session_id": session_id,
                "action": action.value,
                "workflow_id": workflow_id,
                "step_id": step_id,
                "next_step_id": next_step_id,
            }
        )

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        """执行已冻结命令，输入为命令与上下文，输出为模型可读的快照结果。"""
        session_id = self._required_string(args.get("session_id"), "prepared session_id")
        if ctx.session_id != session_id:
            raise ValueError("prepared task progress command session does not match ToolContext")
        action = self._parse_action(args.get("action"))
        workflow_id = self._required_string(args.get("workflow_id"), "prepared workflow_id")
        step_id = self._required_string(args.get("step_id"), "prepared step_id")
        next_step_id = self._optional_string(args.get("next_step_id"), "prepared next_step_id")
        if action is TaskProgressAction.START:
            snapshot = self._manager.start_llm_step(session_id, workflow_id, step_id)
        else:
            snapshot = self._manager.advance_llm_step(
                session_id,
                workflow_id,
                step_id,
                next_step_id,
            )
        counts = snapshot.counts
        data = snapshot.model_dump(mode="json")
        return (
            "task progress advanced: "
            f"{counts.completed}/{counts.total} completed, "
            f"{counts.failed} failed, {counts.cancelled} cancelled",
            data,
        )

    def _parse_action(self, value: object) -> TaskProgressAction:
        """解析有限命令枚举，输入为原始值，输出为 TaskProgressAction。"""
        if not isinstance(value, str):
            raise ValueError("action must be start or next")
        try:
            return TaskProgressAction(value)
        except ValueError as exc:
            raise ValueError("action must be start or next") from exc

    def _required_string(self, value: object, field_name: str) -> str:
        """读取必填文本，输入为原始值和字段名，输出为去空白字符串。"""
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a non-empty string")
        return value.strip()

    def _optional_string(self, value: object, field_name: str) -> str | None:
        """读取可选文本，输入为原始值和字段名，输出为去空白字符串或空。"""
        if value is None:
            return None
        return self._required_string(value, field_name)


def build_task_progress_tool(manager: SessionTaskProgressManager) -> TaskProgressTool:
    """构造任务进度工具，输入为 Manager，输出为受限命令工具。"""
    return TaskProgressTool(manager)


def build_task_progress_tool_from_config(config: Config) -> TaskProgressTool:
    """按 Config 构造工具，输入为配置，输出为受限命令工具。"""
    return build_task_progress_tool(SessionTaskProgressManager.from_config(config))


__all__ = [
    "TaskProgressTool",
    "build_task_progress_tool",
    "build_task_progress_tool_from_config",
]
