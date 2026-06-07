"""Tool entrypoint for agent workflow orchestration."""

from __future__ import annotations

from typing import Any

from core.contracts import ToolContext
from tools.base import BaseBuiltinTool

_SCOPED_WORKDIR_MODE = "scoped_workdir"
_SCOPED_TOOL_NAMES = frozenset({"read_file", "write_file", "list_dir"})
_MAP_REDUCE_SPEC_WRAPPER = "MapReduceWorkflowSpec"
_MAP_REDUCE_REQUIRED_PAYLOAD_KEYS = frozenset(
    {
        "objective",
        "input_source",
        "shard_strategy",
        "mapper",
        "reducer",
        "limits",
        "output_contract",
    }
)


class AgentWorkflowHandle:
    """Mutable binding used by CLI after NativeRuntime is built."""

    def __init__(self) -> None:
        self.manager: Any | None = None

    def bind(self, manager: Any) -> None:
        self.manager = manager


class RunParallelSubagentsTool(BaseBuiltinTool):
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
                        "skill_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional child-agent skill names recorded in audit.",
                        },
                        "permission": {
                            "type": "object",
                            "description": "Child-agent permission. V1 only supports scoped_workdir.",
                            "properties": {
                                "mode": {
                                    "type": "string",
                                    "enum": [_SCOPED_WORKDIR_MODE],
                                }
                            },
                            "required": ["mode"],
                        },
                    },
                    "required": ["task_name", "prompt", "permission"],
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


class RunAgentWorkflowTool(BaseBuiltinTool):
    """Run a registered agent workflow strategy with a JSON payload."""

    name = "run_agent_workflow"
    description = (
        "按 mode 执行已注册的 agent workflow 策略。"
        "mode='parallel' 用于任务并行扇出；mode='map_reduce' 用于结构化分片分析。"
        "map_reduce 的 payload 顶层必须直接包含 objective、input_source、shard_strategy、"
        "mapper、reducer、limits、output_contract；不要把这些字段包在 MapReduceWorkflowSpec 里。"
    )
    input_schema: dict[str, Any] = {  # noqa: RUF012
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["parallel", "map_reduce"],
                "description": "Workflow orchestration mode.",
            },
            "payload": {
                "type": "object",
                "description": (
                    "策略参数。parallel 使用 task_specs 或 tasks；map_reduce 的 payload 顶层"
                    "直接使用 MapReduceWorkflowSpec 字段：objective、input_source、"
                    "shard_strategy、mapper、reducer、limits、output_contract。"
                    '不要写成 {"MapReduceWorkflowSpec": {...}}。'
                ),
            },
        },
        "required": ["mode", "payload"],
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
        mode = args.get("mode")
        if not isinstance(mode, str) or not mode.strip():
            raise ValueError("'mode' must be a non-empty string")
        payload = args.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("'payload' must be an object")
        normalized_payload = _normalize_workflow_payload(mode.strip(), payload)
        result = await manager.run_workflow_payload(
            mode=mode.strip(),
            parent_session_id=ctx.session_id,
            payload=normalized_payload,
        )
        return _format_result(result), _result_data(result)

    def _format_failure_content(self, *, stage: str, error_message: str) -> str:
        """生成 run_agent_workflow 专用中文失败提示，输入为阶段和错误，输出为模型可读内容。"""
        base = super()._format_failure_content(stage=stage, error_message=error_message)
        return (
            f"{base}\n\n"
            "run_agent_workflow 参数修正提示：\n"
            "1. map_reduce payload 的顶层必须直接包含 objective、input_source、"
            "shard_strategy、mapper、reducer、limits、output_contract。\n"
            '2. 禁止把 payload 写成 {"MapReduceWorkflowSpec": {...}} 这种外层包裹。\n'
            "3. 如果本次失败发生在参数校验阶段，表示 workflow 没有启动，"
            "没有子 agent、mapper、reducer 或审计产物。\n"
            "4. 重新调用前先按下面骨架修正参数：\n"
            "{\n"
            '  "mode": "map_reduce",\n'
            '  "payload": {\n'
            '    "objective": "用一句话描述分析目标",\n'
            '    "input_source": {"kind": "path_glob", "root_dir": ".", '
            '"include": ["src/**/*.py"], "exclude": [".venv/**"], '
            '"files": [], "index_provider": "rg", "input_digest": null},\n'
            '    "shard_strategy": {"kind": "by_file_count", "max_files_per_shard": 8, '
            '"max_estimated_tokens_per_shard": 20000, "min_shards": 1, '
            '"max_shards": 8, "preserve_directory_boundary": true, '
            '"prefer_dependency_cohesion": false},\n'
            '    "mapper": {"name_prefix": "map", "prompt_template": "code_findings_v0_1", '
            '"tool_names": ["read_file", "list_dir"], "skill_names": [], '
            '"permission_mode": "scoped_workdir", "max_turns": 3, '
            '"max_output_chars": 60000},\n'
            '    "reducer": {"kind": "deterministic", "dedupe_strategy": "exact_dedupe_key", '
            '"ranking_strategy": "severity_first", "max_findings": 50, '
            '"include_failed_shards": true, "reducer_prompt_template": null},\n'
            '    "limits": {"max_concurrency": 4, "workflow_timeout_seconds": 1800, '
            '"mapper_timeout_seconds": 300, "reducer_timeout_seconds": 300, '
            '"mapper_retries": 0, "validation_repair_retries": 0},\n'
            '    "output_contract": "code_findings"\n'
            "  }\n"
            "}"
        )


AgentWorkflowTool = RunParallelSubagentsTool


def build_agent_workflow_tool(handle: AgentWorkflowHandle) -> RunParallelSubagentsTool:
    return RunParallelSubagentsTool(handle)


def build_run_agent_workflow_tool(handle: AgentWorkflowHandle) -> RunAgentWorkflowTool:
    return RunAgentWorkflowTool(handle)


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
        normalized_tool_names = [name for name in tool_names if name]
        invalid_tool_names = sorted(set(normalized_tool_names) - _SCOPED_TOOL_NAMES)
        if invalid_tool_names:
            raise ValueError(
                f"tasks[{index}].tool_names contains unsupported scoped_workdir tools: "
                f"{invalid_tool_names}"
            )
        skill_names = item.get("skill_names", [])
        if skill_names is None:
            skill_names = []
        if not isinstance(skill_names, list) or not all(
            isinstance(name, str) for name in skill_names
        ):
            raise ValueError(f"tasks[{index}].skill_names must be a list of strings")
        permission = item.get("permission")
        if not isinstance(permission, dict):
            raise ValueError(f"tasks[{index}].permission must be an object")
        if permission.get("mode") != _SCOPED_WORKDIR_MODE:
            raise ValueError(f"tasks[{index}].permission.mode must be scoped_workdir")
        tasks.append(
            {
                "task_name": task_name.strip(),
                "prompt": prompt.strip(),
                "context": context.strip(),
                "tool_names": normalized_tool_names,
                "skill_names": [name for name in skill_names if name],
                "permission": {"mode": _SCOPED_WORKDIR_MODE},
            }
        )
    return tasks


def _normalize_workflow_payload(mode: str, payload: dict[str, Any]) -> dict[str, object]:
    if mode == "parallel":
        raw_tasks = payload.get("task_specs", payload.get("tasks"))
        return {"task_specs": _parse_tasks(raw_tasks)}
    if mode == "map_reduce":
        payload = _unwrap_map_reduce_spec_payload(payload)
    normalized = dict(payload)
    normalized.setdefault("mode", mode)
    return normalized


def _unwrap_map_reduce_spec_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """解包模型误生成的 MapReduceWorkflowSpec 外层，输入为 payload，输出为规范 payload。"""
    nested = payload.get(_MAP_REDUCE_SPEC_WRAPPER)
    if not isinstance(nested, dict):
        return payload
    has_required_top_level = any(key in payload for key in _MAP_REDUCE_REQUIRED_PAYLOAD_KEYS)
    if has_required_top_level:
        return payload
    return dict(nested)


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
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        map_reduce = data.get("map_reduce")
        if isinstance(map_reduce, dict):
            artifact_paths = map_reduce.get("artifact_paths")
            if isinstance(artifact_paths, dict):
                reducer_path = artifact_paths.get("reducer_result_path")
                if isinstance(reducer_path, str):
                    lines.extend(["", f"map_reduce_reducer_result: {reducer_path}"])
    return "\n".join(lines)


def _result_data(result: Any) -> dict[str, Any]:
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
    extra = getattr(result, "data", None)
    if isinstance(extra, dict):
        data.update(extra)
    return data


__all__ = [
    "AgentWorkflowHandle",
    "AgentWorkflowTool",
    "RunAgentWorkflowTool",
    "RunParallelSubagentsTool",
    "build_agent_workflow_tool",
    "build_run_agent_workflow_tool",
]
