"""Tool entrypoint for agent workflow orchestration."""

from __future__ import annotations

from typing import Any

from core.contracts import ToolContext
from tools.base import BaseBuiltinTool


class AgentWorkflowHandle:
    """Mutable binding used by CLI after NativeRuntime is built."""

    def __init__(self) -> None:
        self.manager: Any | None = None

    def bind(self, manager: Any) -> None:
        self.manager = manager


class AgentWorkflowTool(BaseBuiltinTool):
    """Run a small parallel sub-agent workflow."""

    name = "run_parallel_subagents"
    description = (
        "Create independent child agents, run their tasks in parallel, and return their reports. "
        "Each task receives only its own prompt, optional context, and optional explicit tools."
    )
    input_schema: dict[str, Any] = {  # noqa: RUF012
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "description": "Tasks to dispatch to independent child agents.",
                "items": {
                    "type": "object",
                    "properties": {
                        "task_name": {"type": "string"},
                        "prompt": {"type": "string"},
                        "context": {"type": "string"},
                        "tool_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional explicit child-agent tool whitelist.",
                        },
                    },
                    "required": ["task_name", "prompt"],
                },
            },
            "mode": {
                "type": "string",
                "enum": ["parallel"],
                "description": "Workflow orchestration mode. V1 implements parallel.",
            },
        },
        "required": ["tasks"],
    }

    def __init__(self, handle: AgentWorkflowHandle) -> None:
        self._handle = handle

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        manager = self._handle.manager
        if manager is None:
            raise RuntimeError("agent workflow manager is not bound")

        task_specs = _parse_tasks(args["tasks"])
        mode = args.get("mode", "parallel")
        if not isinstance(mode, str):
            raise ValueError("'mode' must be a string")
        result = await manager.run_workflow_specs(
            mode=mode,
            parent_session_id=ctx.session_id,
            task_specs=task_specs,
        )
        content = _format_result(result)
        data = {
            "workflow_id": result.workflow_id,
            "mode": result.mode,
            "workflow_dir": str(result.workflow_dir),
            "report_index_path": str(result.report_index_path),
            "completed": result.completed,
            "reports": [
                {
                    "display_order": report.display_order,
                    "task_id": report.task_id,
                    "task_name": report.task_name,
                    "status": report.status,
                    "summary": report.summary,
                    "error_message": report.error_message,
                    "report_path": report.report_path,
                    "working_dir": report.working_dir,
                    "session_id": report.session_id,
                    "run_id": report.run_id,
                    "reported_at": report.reported_at,
                }
                for report in result.reports
            ],
            "runs": [
                {
                    "task_id": run.task.task_id,
                    "task_name": run.task.task_name,
                    "session_id": run.session_id,
                    "run_id": run.run_id,
                    "status": run.status,
                    "content": run.content,
                    "error_message": run.error_message,
                    "working_dir": run.task.metadata.get("working_dir"),
                }
                for run in result.runs
            ],
        }
        return content, data


def build_agent_workflow_tool(handle: AgentWorkflowHandle) -> AgentWorkflowTool:
    return AgentWorkflowTool(handle)


def _parse_tasks(raw: Any) -> list[dict[str, object]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("'tasks' must be a non-empty list")
    if len(raw) > 8:
        raise ValueError("parallel workflow supports at most 8 tasks in v1")

    tasks: list[dict[str, object]] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"tasks[{index}] must be an object")
        task_name = item.get("task_name")
        prompt = item.get("prompt")
        if not isinstance(task_name, str) or not task_name.strip():
            raise ValueError(f"tasks[{index}].task_name must be a non-empty string")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"tasks[{index}].prompt must be a non-empty string")
        context = item.get("context", "")
        if context is None:
            context = ""
        if not isinstance(context, str):
            raise ValueError(f"tasks[{index}].context must be a string")
        tool_names = item.get("tool_names", [])
        if tool_names is None:
            tool_names = []
        if not isinstance(tool_names, list) or not all(
            isinstance(name, str) for name in tool_names
        ):
            raise ValueError(f"tasks[{index}].tool_names must be a list of strings")
        tasks.append(
            {
                "task_name": task_name.strip(),
                "prompt": prompt.strip(),
                "context": context.strip(),
                "tool_names": [name for name in tool_names if name],
            }
        )
    return tasks


def _format_result(result: Any) -> str:
    lines = [
        f"workflow_id: {result.workflow_id}",
        f"workflow_dir: {result.workflow_dir}",
        f"report_index: {result.report_index_path}",
        f"completed: {result.completed}",
        "",
        "subagent reports:",
    ]
    for report in result.reports:
        lines.append(f"- {report.task_name} [{report.status}] session={report.session_id}")
        lines.append(f"  summary: {report.summary}")
        lines.append(f"  error_message: {report.error_message}")
        lines.append(f"  working_dir: {report.working_dir}")
        lines.append(f"  report: {report.report_path}")
    lines.extend(
        [
            "",
            "Use these child-agent reports to synthesize the final answer, "
            "including successes, failures, and follow-up risks.",
        ]
    )
    return "\n".join(lines)


__all__ = ["AgentWorkflowHandle", "AgentWorkflowTool", "build_agent_workflow_tool"]
