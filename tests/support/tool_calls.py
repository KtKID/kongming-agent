"""测试中的 Tool 直调助手。

所有测试直调都复刻 Runner 的合同边界：先且只先 prepare 一次，再把独立的
PreparedToolCall 快照交给 execute。该助手不处理审批与错误翻译。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from core.contracts import (
    PreparedToolCall,
    Tool,
    ToolCallPreparer,
    ToolContext,
    ToolResult,
)


async def execute_prepared_tool(
    tool: Tool,
    arguments: dict[str, Any],
    context: ToolContext,
) -> ToolResult:
    """准备并执行一次测试 Tool 调用，返回结构化 ToolResult。"""
    try:
        prepared = (
            tool.prepare(deepcopy(arguments), context)
            if isinstance(tool, ToolCallPreparer)
            else PreparedToolCall(arguments=deepcopy(arguments))
        )
    except Exception as exc:
        error_message = str(exc)
        formatter = getattr(tool, "_format_failure_content", None)
        mode_formatter = getattr(tool, "_format_failure_content_for_mode", None)
        raw_mode = arguments.get("mode") if isinstance(arguments, dict) else None
        mode = raw_mode if isinstance(raw_mode, str) else None
        content = (
            mode_formatter(
                mode=mode,
                stage="参数校验",
                error_message=f"argument validation failed: {error_message}",
            )
            if callable(mode_formatter)
            else formatter(
                stage="参数校验",
                error_message=f"argument validation failed: {error_message}",
            )
            if callable(formatter)
            else ""
        )
        return ToolResult(
            ok=False,
            content=content,
            error_message=error_message,
        )
    return await tool.execute(deepcopy(prepared), context)


__all__ = ["execute_prepared_tool"]
