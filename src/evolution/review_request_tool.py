"""主 Agent 显式请求当前 run 结束后执行进化审查的公开 Tool。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from core.contracts import PreparedToolCall, ToolContext, ToolResult
from evolution.models import (
    MAX_MANUAL_REVIEW_FOCUS_CHARS,
    EvolutionReviewTrigger,
    ManualReviewQueueStatus,
)

if TYPE_CHECKING:
    from evolution.evolution_manager import EvolutionManager

REQUEST_EVOLUTION_REVIEW_TOOL_NAME = "request_evolution_review"


class RequestEvolutionReviewTool:
    """登记当前 run 的一次幂等进化审查请求。"""

    name = REQUEST_EVOLUTION_REVIEW_TOOL_NAME
    description = (
        "当用户明确要求复盘当前对话、沉淀经验或提炼可复用流程时，调用本工具。"
        "工具会登记一次请求，并在当前 run 成功结束后使用完整对话和最终回答启动审查。"
        "审查只生成候选养料，最终由用户选择采纳为 memory、skill 或忽略。"
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "focus": {
                "type": "string",
                "maxLength": MAX_MANUAL_REVIEW_FOCUS_CHARS,
                "description": "本轮复盘希望重点提炼的经验或流程，可省略。",
            },
        },
        "additionalProperties": False,
    }

    def __init__(self, manager: EvolutionManager) -> None:
        self._manager = manager

    def _validate_args(self, args: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise TypeError(f"tool args must be dict, got {type(args).__name__}")
        unknown = set(args) - {"focus"}
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"unknown arguments: {names}")
        focus = args.get("focus")
        if focus is None:
            return {}
        if not isinstance(focus, str):
            raise TypeError("focus must be a string")
        normalized = focus.strip()
        if len(normalized) > MAX_MANUAL_REVIEW_FOCUS_CHARS:
            raise ValueError(f"focus must be at most {MAX_MANUAL_REVIEW_FOCUS_CHARS} characters")
        return {"focus": normalized or None}

    def prepare(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> PreparedToolCall:
        """审批前校验并冻结规范化 focus。"""
        del context
        return PreparedToolCall(arguments=self._validate_args(arguments))

    async def execute(
        self,
        prepared: PreparedToolCall,
        ctx: ToolContext,
    ) -> ToolResult:
        """消费已准备 focus 并把当前 run 的审查意图登记到 Manager。"""
        try:
            status = await self._manager.queue_manual_review(
                session_id=ctx.session_id,
                run_id=ctx.run_id,
                focus=prepared.arguments.get("focus"),
            )
        except Exception as exc:
            error_message = str(exc)
            return ToolResult(
                ok=False,
                content=f"进化审查请求登记失败：{error_message}",
                error_message=error_message,
            )
        data: dict[str, Any] = {
            "status": status.value,
            "session_id": ctx.session_id,
            "run_id": ctx.run_id,
            "trigger_reason": EvolutionReviewTrigger.MANUAL_TOOL.value,
        }
        if status is ManualReviewQueueStatus.ALREADY_QUEUED:
            return ToolResult(
                ok=True,
                content="当前 run 已登记进化审查，本轮成功结束后只会执行一次。",
                data=data,
            )
        return ToolResult(
            ok=True,
            content="已登记进化审查，将在当前 run 成功结束后执行。",
            data=data,
        )


def build_request_evolution_review_tool(
    manager: EvolutionManager,
) -> RequestEvolutionReviewTool:
    """构造绑定同一 EvolutionManager 状态 owner 的公开审查 Tool。"""
    return RequestEvolutionReviewTool(manager)


__all__ = [
    "REQUEST_EVOLUTION_REVIEW_TOOL_NAME",
    "RequestEvolutionReviewTool",
    "build_request_evolution_review_tool",
]
