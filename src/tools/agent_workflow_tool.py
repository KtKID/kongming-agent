"""Agent workflow 编排工具入口。

本脚本负责把 LLM tool call 转换为 AgentWorkflowManager 可执行的 workflow 请求。
作用是向主 agent 暴露 run_agent_workflow 通用策略入口和 run_parallel_subagents
兼容入口，并在工具层完成参数基础校验、常见模型参数形态归一化和结果格式化。
关键执行流程：解析 mode/payload，归一化 parallel 或 map_reduce 参数，通过 late-bound
AgentWorkflowHandle 调用 manager，再把 workflow 产物路径、子 agent 报告和结构化 data
返回给 runner。
关键函数：_normalize_workflow_payload 负责策略 payload 归一化，_parse_tasks 负责 parallel
任务校验，_format_result/_result_data 负责 tool result 投影。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.contracts import ToolContext, ToolResult
from tools.runtime.base import BaseBuiltinTool

_SCOPED_WORKDIR_MODE = "scoped_workdir"
_SCOPED_TOOL_NAMES = frozenset({"read_file", "write_file", "list_dir"})
_INLINE_INPUT_PREFIXES = ("noop://", "inline://")
_TEMP_INLINE_INPUT_ROOTS = ("/tmp/", "/private/tmp/", "/var/tmp/")
_MAP_REDUCE_SPEC_WRAPPER = "MapReduceWorkflowSpec"
_ROUNDTABLE_REVIEW_SPEC_WRAPPER = "RoundtableReviewSpec"
_DEEP_RESEARCH_SPEC_WRAPPER = "DeepResearchSpec"
_WORKFLOW_DESC_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": "一句 workflow 简短描述，用于 Workflow Viewer 展示，建议 20-60 个中文字符。",
    "maxLength": 120,
}
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
_NULL_STRINGS = frozenset({"", "null", "none"})

_MAP_REDUCE_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "map_reduce payload 顶层直接包含这些字段。数组必须写 JSON array，"
        "整数必须写 number，布尔必须写 true/false，null 必须写 JSON null。"
    ),
    "properties": {
        "mode": {"type": "string", "enum": ["map_reduce"]},
        "objective": {"type": "string"},
        "input_source": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["path_glob", "file_list"]},
                "root_dir": {"type": "string"},
                "include": {"type": "array", "items": {"type": "string"}},
                "exclude": {"type": "array", "items": {"type": "string"}},
                "files": {"type": "array", "items": {"type": "string"}},
                "index_provider": {"type": ["string", "null"]},
                "input_digest": {"type": ["string", "null"]},
            },
        },
        "shard_strategy": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["by_directory", "by_file_count"]},
                "max_files_per_shard": {"type": "integer"},
                "max_estimated_tokens_per_shard": {"type": "integer"},
                "min_shards": {"type": "integer"},
                "max_shards": {"type": "integer"},
                "preserve_directory_boundary": {"type": "boolean"},
                "prefer_dependency_cohesion": {"type": "boolean"},
            },
        },
        "mapper": {
            "type": "object",
            "properties": {
                "name_prefix": {"type": "string"},
                "prompt_template": {"type": "string"},
                "tool_names": {"type": "array", "items": {"type": "string"}},
                "skill_names": {"type": "array", "items": {"type": "string"}},
                "permission_mode": {"type": "string", "enum": [_SCOPED_WORKDIR_MODE]},
                "max_turns": {"type": "integer"},
                "max_output_chars": {"type": "integer"},
            },
        },
        "reducer": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["deterministic"]},
                "dedupe_strategy": {
                    "type": "string",
                    "enum": ["exact_dedupe_key", "file_line_title"],
                },
                "ranking_strategy": {
                    "type": "string",
                    "enum": ["severity_first", "confidence_first", "impact_first"],
                },
                "max_findings": {"type": "integer"},
                "include_failed_shards": {"type": "boolean"},
                "reducer_prompt_template": {"type": ["string", "null"]},
            },
        },
        "limits": {
            "type": "object",
            "properties": {
                "max_concurrency": {"type": "integer"},
                "workflow_timeout_seconds": {"type": "integer"},
                "mapper_timeout_seconds": {"type": "integer"},
                "reducer_timeout_seconds": {"type": "integer"},
                "mapper_retries": {"type": "integer"},
                "validation_repair_retries": {"type": "integer"},
            },
        },
        "output_contract": {"type": "string", "enum": ["code_findings", "raw_text"]},
        "audit_tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "objective",
        "input_source",
        "shard_strategy",
        "mapper",
        "reducer",
        "limits",
        "output_contract",
    ],
}

_ROUNDTABLE_REVIEW_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "roundtable_review payload 顶层直接包含 topic、input_source、limits 等字段。"
        "participants 子对象只支持 select 数组；discussion_rounds 包含第 1 轮独立分析。"
    ),
    "properties": {
        "mode": {"type": "string", "enum": ["roundtable_review"]},
        "topic": {"type": "string"},
        "objective": {"type": "string"},
        "module_path": {"type": ["string", "array"]},
        "input_source": {
            "type": "object",
            "properties": {
                "root_dir": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}},
                "include": {"type": "array", "items": {"type": "string"}},
                "exclude": {"type": "array", "items": {"type": "string"}},
                "max_files": {"type": "integer"},
                "max_bytes_per_file": {"type": "integer"},
            },
        },
        "participants": {
            "type": "object",
            "description": "圆桌子 agent 角色选择，只支持 select。",
            "properties": {
                "select": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "已保存或内置的 agent role id 列表。",
                }
            },
            "required": ["select"],
        },
        "limits": {
            "type": "object",
            "properties": {
                "total_child_token_budget": {"type": "integer"},
                "discussion_rounds": {"type": "integer"},
                "max_discussion_rounds": {"type": "integer"},
                "max_concurrency": {"type": "integer"},
                "reviewer_max_turns": {"type": "integer"},
                "arbiter_max_turns": {"type": "integer"},
                "agent_timeout_seconds": {"type": "integer"},
            },
        },
        "audit_tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["topic"],
}

_DEEP_RESEARCH_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "deep_research payload 顶层直接包含 topic、source_queries、limits 和 source_fixture，"
        "也接受 search_plan.lines 与 source_provider_fixture 别名。"
    ),
    "properties": {
        "mode": {"type": "string", "enum": ["deep_research"]},
        "topic": {"type": "string"},
        "objective": {"type": "string"},
        "search_plan": {
            "type": "object",
            "properties": {
                "lines": {"type": "array", "items": {"type": ["string", "object"]}},
            },
        },
        "source_queries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "query_id": {"type": "string"},
                    "line": {"type": "string"},
                    "intent": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
            },
        },
        "limits": {
            "type": "object",
            "properties": {
                "source_budget": {"type": "integer", "default": 10},
                "fetch_budget": {"type": "integer", "default": 10},
                "fact_cap": {"type": "integer", "default": 20},
                "jury_size": {"type": "integer", "default": 3},
                "reject_quorum": {"type": "integer", "default": 2},
                "max_content_chars": {"type": "integer", "default": 60000},
                "search_results_per_line": {"type": "integer", "default": 6},
                "fetch_concurrency": {"type": "integer", "default": 4},
                "jury_concurrency": {"type": "integer", "default": 6},
                "workflow_timeout_seconds": {"type": "integer", "default": 2400},
            },
        },
        "source_policy": {
            "type": "object",
            "properties": {
                "provider": {"type": "string", "enum": ["fake", "internal"]},
                "language": {"type": "string", "default": "zh-CN"},
                "freshness_days": {"type": ["integer", "null"], "default": None},
                "allowed_domains": {"type": "array", "items": {"type": "string"}},
                "blocked_domains": {"type": "array", "items": {"type": "string"}},
                "prefer_primary_sources": {"type": "boolean", "default": True},
            },
        },
        "output_contract": {"type": "string", "enum": ["deep_research_report"]},
        "source_fixture": {"type": "object"},
        "source_provider_fixture": {"type": "object"},
        "audit_tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["topic"],
}


class AgentWorkflowHandle:
    """工作流 manager 的延迟绑定句柄，输入为 runtime 装配结果，输出为 tool 可用 manager。"""

    def __init__(self) -> None:
        self.manager: Any | None = None
        self._managers_by_session_id: dict[str, Any] = {}

    def bind(self, manager: Any, *, session_id: str | None = None) -> None:
        """绑定 workflow manager；session_id 为空时写入默认 manager，有值时写入 thread 专属 manager。"""
        if session_id is None:
            self.manager = manager
            return
        self._managers_by_session_id[session_id] = manager

    def get(self, ctx: ToolContext) -> Any | None:
        """按 ToolContext 查找 manager；优先返回当前 session 的绑定，其次返回默认绑定。"""
        return self._managers_by_session_id.get(ctx.session_id) or self.manager


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
            "desc": _WORKFLOW_DESC_SCHEMA,
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
        manager = self._handle.get(ctx)
        if manager is None:
            raise RuntimeError("agent workflow manager is not bound")

        task_specs = _parse_tasks(args["tasks"])
        mode = args.get("mode", "parallel")
        if not isinstance(mode, str):
            raise ValueError("'mode' must be a string")
        kwargs: dict[str, Any] = {
            "mode": mode,
            "parent_session_id": ctx.session_id,
            "task_specs": task_specs,
        }
        if isinstance(args.get("desc"), str):
            kwargs["desc"] = args["desc"]
        result = await manager.run_workflow_specs(**kwargs)
        content = _format_result(result)
        data = {
            "workflow_id": result.workflow_id,
            "mode": result.mode,
            "desc": getattr(result, "desc", None),
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
        "mode='parallel' 用于任务并行扇出；mode='map_reduce' 用于结构化分片分析；"
        "mode='roundtable_review' 用于多 Agent 圆桌评审；"
        "mode='deep_research' 用于带来源、事实和投票比分的深度研究。"
        "map_reduce 的 payload 顶层必须直接包含 objective、input_source、shard_strategy、"
        "mapper、reducer、limits、output_contract；不要把这些字段包在 MapReduceWorkflowSpec 里。"
    )
    input_schema: dict[str, Any] = {  # noqa: RUF012
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["parallel", "map_reduce", "roundtable_review", "deep_research"],
                "description": "Workflow orchestration mode.",
            },
            "payload": {
                "type": "object",
                "description": (
                    "策略参数。parallel 使用 task_specs 或 tasks；map_reduce 的 payload 顶层"
                    "直接使用 MapReduceWorkflowSpec 字段：objective、input_source、"
                    "shard_strategy、mapper、reducer、limits、output_contract。"
                    "roundtable_review 必须使用 participants.select 选择角色，"
                    "不要使用 reviewers。"
                    '不要写成 {"MapReduceWorkflowSpec": {...}}。'
                ),
                "properties": {
                    "desc": _WORKFLOW_DESC_SCHEMA,
                    "task_specs": {
                        "type": "array",
                        "description": "parallel 任务规格数组。",
                    },
                    "tasks": {
                        "type": "array",
                        "description": "parallel 兼容任务数组。",
                    },
                    **_MAP_REDUCE_PAYLOAD_SCHEMA["properties"],
                    **_ROUNDTABLE_REVIEW_PAYLOAD_SCHEMA["properties"],
                    **_DEEP_RESEARCH_PAYLOAD_SCHEMA["properties"],
                },
            },
        },
        "required": ["mode", "payload"],
    }

    def __init__(self, handle: AgentWorkflowHandle) -> None:
        self._handle = handle

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行 workflow 工具，输入为模型参数和上下文，输出为结构化 ToolResult。"""
        mode = (
            args.get("mode")
            if isinstance(args, dict) and isinstance(args.get("mode"), str)
            else None
        )
        try:
            validated = self._validate_args(args)
        except Exception as exc:
            error_message = f"argument validation failed: {exc}"
            return ToolResult(
                ok=False,
                content=self._format_failure_content_for_mode(
                    mode=mode,
                    stage="参数校验",
                    error_message=error_message,
                ),
                error_message=error_message,
            )

        try:
            content, data = await self._run(validated, ctx)
        except Exception as exc:
            error_message = str(exc)
            return ToolResult(
                ok=False,
                content=self._format_failure_content_for_mode(
                    mode=mode,
                    stage="工具执行",
                    error_message=error_message,
                ),
                error_message=error_message,
            )

        return ToolResult(ok=True, content=content, data=data)

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        manager = self._handle.get(ctx)
        if manager is None:
            raise RuntimeError("agent workflow manager is not bound")
        mode = args.get("mode")
        if not isinstance(mode, str) or not mode.strip():
            raise ValueError("'mode' must be a non-empty string")
        payload = args.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("'payload' must be an object")
        normalized_payload = _normalize_workflow_payload(
            mode.strip(),
            payload,
            workspace_root=_workflow_workspace_root(manager, ctx),
            tool_context=ctx,
        )
        result = await manager.run_workflow_payload(
            mode=mode.strip(),
            parent_session_id=ctx.session_id,
            payload=normalized_payload,
        )
        return _format_result(result), _result_data(result)

    def _format_failure_content(self, *, stage: str, error_message: str) -> str:
        """生成 run_agent_workflow 专用中文失败提示，输入为阶段和错误，输出为模型可读内容。"""
        return self._format_failure_content_for_mode(
            mode=None,
            stage=stage,
            error_message=error_message,
        )

    def _format_failure_content_for_mode(
        self,
        *,
        mode: str | None,
        stage: str,
        error_message: str,
    ) -> str:
        """按 workflow mode 生成失败提示，输入为 mode/阶段/错误，输出为模型可读内容。"""
        base = super()._format_failure_content(stage=stage, error_message=error_message)
        if mode == "deep_research":
            return (
                f"{base}\n\n"
                "run_agent_workflow deep_research 参数修正提示：\n"
                "1. deep_research payload 顶层必须包含 topic；objective 可省略，省略时使用 topic。\n"
                "2. source_queries 是研究问题数组；缺省时会根据 topic 生成 overview、primary_source、risks 三条查询。\n"
                "3. limits 控制 jury_size、reject_quorum、source_budget、fetch_budget、fact_cap 等预算。\n"
                "4. source_policy 控制 provider、language、freshness_days、allowed_domains、blocked_domains、prefer_primary_sources。\n"
                "5. output_contract 固定为 deep_research_report。\n"
                "6. 重新调用前先按下面骨架修正参数：\n"
                "{\n"
                '  "mode": "deep_research",\n'
                '  "payload": {\n'
                '    "topic": "研究主题",\n'
                '    "objective": "可省略；省略时使用 topic",\n'
                '    "source_queries": [\n'
                '      {"query_id": "q1", "line": "研究主题 overview", '
                '"intent": "overview", "max_results": 3}\n'
                "    ],\n"
                '    "limits": {"jury_size": 3, "reject_quorum": 2, '
                '"source_budget": 10, "fetch_budget": 10, "fact_cap": 20},\n'
                '    "source_policy": {"provider": "fake", "language": "zh-CN", '
                '"freshness_days": null, "allowed_domains": [], '
                '"blocked_domains": [], "prefer_primary_sources": true},\n'
                '    "output_contract": "deep_research_report"\n'
                "  }\n"
                "}"
            )
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


def _normalize_workflow_payload(
    mode: str,
    payload: dict[str, Any],
    *,
    workspace_root: Path | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, object]:
    if mode == "parallel":
        raw_tasks = payload.get("task_specs", payload.get("tasks"))
        normalized: dict[str, object] = {"task_specs": _parse_tasks(raw_tasks)}
        desc = payload.get("desc")
        if isinstance(desc, str):
            normalized["desc"] = desc
        return normalized
    if mode == "map_reduce":
        payload = _unwrap_map_reduce_spec_payload(payload)
        payload = _normalize_map_reduce_payload(
            payload,
            workspace_root=workspace_root,
            tool_context=tool_context,
        )
    if mode == "roundtable_review":
        payload = _unwrap_roundtable_review_spec_payload(payload)
        payload = _normalize_roundtable_review_payload(payload)
    if mode == "deep_research":
        payload = _unwrap_deep_research_spec_payload(payload)
        payload = _normalize_deep_research_payload(payload)
    normalized = dict(payload)
    normalized.setdefault("mode", mode)
    return normalized


def _normalize_map_reduce_payload(
    payload: dict[str, Any],
    *,
    workspace_root: Path | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """归一化 map_reduce 参数，输入为模型生成 payload，输出为 parser 友好的 payload。"""
    normalized = dict(payload)
    root = workspace_root or Path.cwd().resolve()
    input_source = _object_copy(normalized.get("input_source"))
    if input_source is not None:
        input_source.setdefault("root_dir", ".")
        for key in ("include", "exclude", "files"):
            input_source[key] = _coerce_string_array(input_source.get(key))
        input_source["files"] = _normalize_map_reduce_file_list(
            input_source.get("files"),
            root_dir=input_source.get("root_dir"),
            workspace_root=root,
        )
        for key in ("index_provider", "input_digest"):
            input_source[key] = _coerce_nullable_string(input_source.get(key))
        normalized["input_source"] = input_source

    shard_strategy = _object_copy(normalized.get("shard_strategy"))
    if shard_strategy is not None:
        shard_strategy.setdefault("max_estimated_tokens_per_shard", 20000)
        shard_strategy.setdefault("min_shards", 1)
        shard_strategy.setdefault("max_shards", 8)
        shard_strategy.setdefault("preserve_directory_boundary", True)
        shard_strategy.setdefault("prefer_dependency_cohesion", False)
        for key in (
            "max_files_per_shard",
            "max_estimated_tokens_per_shard",
            "min_shards",
            "max_shards",
        ):
            shard_strategy[key] = _coerce_int(shard_strategy.get(key))
        for key in ("preserve_directory_boundary", "prefer_dependency_cohesion"):
            shard_strategy[key] = _coerce_bool(shard_strategy.get(key))
        if shard_strategy.get("prefer_dependency_cohesion") is True:
            shard_strategy["prefer_dependency_cohesion"] = False
        normalized["shard_strategy"] = shard_strategy

    mapper = _object_copy(normalized.get("mapper"))
    if mapper is not None:
        mapper.setdefault("skill_names", [])
        mapper.setdefault("permission_mode", _SCOPED_WORKDIR_MODE)
        for key in ("tool_names", "skill_names"):
            mapper[key] = _coerce_string_array(mapper.get(key))
        mapper["tool_names"] = _normalize_map_reduce_tool_names(mapper.get("tool_names"))
        for key in ("max_turns", "max_output_chars"):
            mapper[key] = _coerce_int(mapper.get(key))
        normalized["mapper"] = mapper

    reducer = _object_copy(normalized.get("reducer"))
    if reducer is not None:
        reducer.setdefault("max_findings", 50)
        reducer.setdefault("include_failed_shards", True)
        reducer["max_findings"] = _coerce_int(reducer.get("max_findings"))
        reducer["include_failed_shards"] = _coerce_bool(reducer.get("include_failed_shards"))
        reducer["reducer_prompt_template"] = _coerce_nullable_string(
            reducer.get("reducer_prompt_template")
        )
        normalized["reducer"] = reducer

    limits = _object_copy(normalized.get("limits"))
    if limits is not None:
        limits.setdefault("max_concurrency", 4)
        limits.setdefault("workflow_timeout_seconds", 1800)
        limits.setdefault("mapper_timeout_seconds", 300)
        limits.setdefault("reducer_timeout_seconds", 300)
        limits.setdefault("mapper_retries", 0)
        limits.setdefault("validation_repair_retries", 0)
        for key in (
            "max_concurrency",
            "workflow_timeout_seconds",
            "mapper_timeout_seconds",
            "reducer_timeout_seconds",
            "mapper_retries",
            "validation_repair_retries",
        ):
            limits[key] = _coerce_int(limits.get(key))
        normalized["limits"] = limits

    _normalize_inline_map_reduce_input(
        normalized,
        workspace_root=root,
        tool_context=tool_context,
    )
    normalized["audit_tags"] = _coerce_string_array(normalized.get("audit_tags"))
    return normalized


def _workflow_workspace_root(manager: Any, ctx: ToolContext) -> Path:
    """解析 workflow 工作区根，输入为 manager/context，输出为绝对目录。"""
    manager_root = getattr(manager, "workspace_root", None)
    if isinstance(manager_root, Path):
        return manager_root.expanduser().resolve()
    if isinstance(manager_root, str) and manager_root.strip():
        return Path(manager_root).expanduser().resolve()
    ctx_root = ctx.metadata.get("cwd")
    if isinstance(ctx_root, str) and ctx_root.strip():
        return Path(ctx_root).expanduser().resolve()
    return Path.cwd().resolve()


def _normalize_map_reduce_file_list(
    value: Any,
    *,
    root_dir: Any,
    workspace_root: Path,
) -> Any:
    """归一化 file_list 路径，输入为模型文件列表，输出为相对 input root 的路径列表。"""
    if not isinstance(value, list):
        return value
    input_root = _resolve_input_root_for_normalization(root_dir, workspace_root)
    return [
        _normalize_map_reduce_file_path(item, input_root=input_root)
        if isinstance(item, str)
        else item
        for item in value
    ]


def _resolve_input_root_for_normalization(root_dir: Any, workspace_root: Path) -> Path:
    """解析归一化用 input root，输入为 root_dir 字段，输出为绝对目录。"""
    if not isinstance(root_dir, str) or not root_dir.strip():
        return workspace_root
    root_path = Path(root_dir).expanduser()
    if root_path.is_absolute():
        return root_path.resolve()
    return (workspace_root / root_path).resolve()


def _normalize_map_reduce_file_path(value: str, *, input_root: Path) -> str:
    """把 workspace 内绝对文件路径转相对路径，输入为文件路径，输出为原值或相对路径。"""
    stripped = value.strip()
    if not stripped:
        return value
    path = Path(stripped).expanduser()
    if not path.is_absolute():
        return stripped
    try:
        return path.resolve().relative_to(input_root).as_posix()
    except ValueError:
        return value


def _normalize_map_reduce_tool_names(value: Any) -> Any:
    """过滤 mapper 工具白名单，输入为模型工具数组，输出为 scoped file tools。"""
    if not isinstance(value, list):
        return value
    normalized = [name for name in value if isinstance(name, str) and name in _SCOPED_TOOL_NAMES]
    return normalized or ["read_file", "list_dir"]


def _normalize_roundtable_review_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """归一化 roundtable_review 参数，输入为模型生成 payload，输出为 parser 友好 payload。"""
    normalized = dict(payload)
    if "reviewers" in normalized:
        raise ValueError("reviewers is removed; use participants.select")
    input_source = _object_copy(normalized.get("input_source"))
    if input_source is None:
        input_source = {}
    input_source.setdefault("root_dir", ".")
    for key in ("paths", "include", "exclude"):
        input_source[key] = _coerce_string_array(input_source.get(key))
    for key in ("max_files", "max_bytes_per_file"):
        if key in input_source:
            input_source[key] = _coerce_int(input_source.get(key))
    normalized["input_source"] = input_source

    if "module_path" in normalized:
        module_path = normalized["module_path"]
        if isinstance(module_path, str):
            normalized["module_path"] = module_path.strip()
        else:
            normalized["module_path"] = _coerce_string_array(module_path)

    participants = _object_copy(normalized.get("participants"))
    if participants is not None:
        if "select" in participants:
            participants["select"] = _coerce_string_array(participants.get("select"))
        normalized["participants"] = participants

    limits = _object_copy(normalized.get("limits"))
    if limits is not None:
        limits.setdefault("total_child_token_budget", 50000)
        limits.setdefault("max_discussion_rounds", 6)
        for key in (
            "total_child_token_budget",
            "discussion_rounds",
            "max_discussion_rounds",
            "max_concurrency",
            "reviewer_max_turns",
            "arbiter_max_turns",
            "agent_timeout_seconds",
        ):
            if key in limits:
                limits[key] = _coerce_int(limits.get(key))
        normalized["limits"] = limits
    normalized["audit_tags"] = _coerce_string_array(normalized.get("audit_tags"))
    return normalized


def _normalize_deep_research_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """归一化 deep_research 参数，输入为模型生成 payload，输出为 parser 友好 payload。"""
    normalized = dict(payload)
    if "source_provider_fixture" in normalized and "source_fixture" not in normalized:
        normalized["source_fixture"] = normalized["source_provider_fixture"]
    objective = normalized.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        topic = normalized.get("topic")
        if isinstance(topic, str) and topic.strip():
            normalized["topic"] = topic.strip()
            normalized["objective"] = topic.strip()
    queries = normalized.get("source_queries")
    if not isinstance(queries, list):
        queries = _deep_research_queries_from_search_plan(normalized.get("search_plan"))
    if isinstance(queries, list):
        normalized["source_queries"] = [
            _normalize_deep_research_query(item, index)
            for index, item in enumerate(queries, 1)
            if isinstance(item, dict)
        ]
    if not normalized.get("source_queries"):
        topic = normalized.get("topic")
        if isinstance(topic, str) and topic.strip():
            normalized["source_queries"] = [
                {
                    "query_id": "q1",
                    "line": topic.strip(),
                    "intent": "overview",
                    "max_results": 3,
                },
                {
                    "query_id": "q2",
                    "line": f"{topic.strip()} primary sources",
                    "intent": "primary_source",
                    "max_results": 3,
                },
                {
                    "query_id": "q3",
                    "line": f"{topic.strip()} risks limitations evidence",
                    "intent": "skeptical",
                    "max_results": 3,
                },
            ]
    limits = _object_copy(normalized.get("limits")) or {}
    limits.setdefault("source_budget", 10)
    limits.setdefault("fetch_budget", 10)
    limits.setdefault("fact_cap", 20)
    limits.setdefault("jury_size", 3)
    limits.setdefault("reject_quorum", 2)
    limits.setdefault("max_content_chars", 60000)
    limits.setdefault("search_results_per_line", 6)
    limits.setdefault("fetch_concurrency", 4)
    limits.setdefault("jury_concurrency", 6)
    limits.setdefault("workflow_timeout_seconds", 2400)
    for key in (
        "source_budget",
        "fetch_budget",
        "fact_cap",
        "jury_size",
        "reject_quorum",
        "max_content_chars",
        "search_results_per_line",
        "fetch_concurrency",
        "jury_concurrency",
        "workflow_timeout_seconds",
    ):
        if key in limits:
            limits[key] = _coerce_int(limits.get(key))
    normalized["limits"] = limits
    source_policy = _object_copy(normalized.get("source_policy")) or {}
    source_policy.setdefault("provider", "fake")
    source_policy.setdefault("language", "zh-CN")
    source_policy.setdefault("freshness_days", None)
    source_policy.setdefault("allowed_domains", [])
    source_policy.setdefault("blocked_domains", [])
    source_policy.setdefault("prefer_primary_sources", True)
    normalized["source_policy"] = source_policy
    normalized.setdefault("output_contract", "deep_research_report")
    if not isinstance(normalized.get("source_fixture"), dict):
        normalized["source_fixture"] = {}
    normalized["audit_tags"] = _coerce_string_array(normalized.get("audit_tags"))
    return normalized


def _deep_research_queries_from_search_plan(value: Any) -> list[dict[str, Any]]:
    """从 search_plan 读取搜索线，输入为任意 payload 字段，输出为 query 字典列表。"""
    plan = _object_copy(value)
    if plan is None:
        return []
    lines = plan.get("lines", plan.get("queries"))
    if isinstance(lines, str):
        lines = [lines]
    if not isinstance(lines, list):
        return []
    queries: list[dict[str, Any]] = []
    for index, item in enumerate(lines, 1):
        if isinstance(item, str):
            queries.append(
                {
                    "query_id": f"q{index}",
                    "line": item,
                    "intent": "overview",
                    "max_results": plan.get("max_results", 3),
                }
            )
        elif isinstance(item, dict):
            queries.append(dict(item))
    return queries


def _normalize_deep_research_query(item: dict[str, Any], index: int) -> dict[str, Any]:
    """归一化单条 deep_research query，输入为 query 映射和序号，输出为稳定 query。"""
    query = dict(item)
    query.setdefault("query_id", f"q{index}")
    query.setdefault("intent", "overview")
    query.setdefault("max_results", 3)
    query["max_results"] = _coerce_int(query.get("max_results"))
    return query


def _normalize_inline_map_reduce_input(
    normalized: dict[str, Any],
    *,
    workspace_root: Path,
    tool_context: ToolContext | None,
) -> None:
    """把 noop/inline 输入转换为真实占位文件，输入为 payload，输出为原地归一化。"""
    input_source = normalized.get("input_source")
    if not isinstance(input_source, dict):
        return
    files = input_source.get("files")
    if not _contains_inline_input(files):
        return
    shard_strategy = normalized.get("shard_strategy")
    count = _inline_shard_count(files, shard_strategy)
    inline_root, rel_files = _write_inline_map_reduce_files(
        workspace_root=workspace_root,
        tool_context=tool_context,
        count=count,
        objective=normalized.get("objective"),
    )
    input_source["kind"] = "file_list"
    input_source["root_dir"] = inline_root
    input_source["include"] = []
    input_source["exclude"] = []
    input_source["files"] = rel_files
    input_source["index_provider"] = input_source.get("index_provider") or "inline"
    normalized["output_contract"] = "raw_text"


def _contains_inline_input(value: Any) -> bool:
    """判断文件列表是否包含 inline 占位符，输入为任意值，输出为布尔值。"""
    if not isinstance(value, list):
        return False
    return any(isinstance(item, str) and _is_inline_input_item(item) for item in value)


def _inline_shard_count(files: Any, shard_strategy: Any) -> int:
    """推断 inline 分片数，输入为 files 和 shard_strategy，输出为至少 1 的数量。"""
    inline_count = 0
    if isinstance(files, list):
        inline_count = sum(
            1 for item in files if isinstance(item, str) and _is_inline_input_item(item)
        )
    min_shards = 1
    max_shards: int | None = None
    if isinstance(shard_strategy, dict):
        raw_min = shard_strategy.get("min_shards")
        raw_max = shard_strategy.get("max_shards")
        if isinstance(raw_min, int) and raw_min > 0:
            min_shards = raw_min
        if isinstance(raw_max, int) and raw_max > 0:
            max_shards = raw_max
    count = max(1, inline_count, min_shards)
    if max_shards is not None:
        count = min(count, max_shards)
    return count


def _is_inline_input_item(value: str) -> bool:
    """识别 inline 输入占位符，输入为模型文件项，输出为是否应生成合成输入。"""
    stripped = value.strip()
    if stripped.startswith(_INLINE_INPUT_PREFIXES):
        return True
    return _is_temporary_absolute_placeholder(stripped)


def _is_temporary_absolute_placeholder(value: str) -> bool:
    """识别模型生成的临时绝对占位路径，输入为路径文本，输出为是否可转 inline。"""
    if not value:
        return False
    path = Path(value).expanduser()
    if not path.is_absolute() or path.exists():
        return False
    candidates = {path.as_posix(), path.resolve(strict=False).as_posix()}
    return any(
        candidate.startswith(root) for candidate in candidates for root in _TEMP_INLINE_INPUT_ROOTS
    )


def _write_inline_map_reduce_files(
    *,
    workspace_root: Path,
    tool_context: ToolContext | None,
    count: int,
    objective: Any,
) -> tuple[str, list[str]]:
    """写入 inline 占位输入，输入为工作区和数量，输出为根目录和相对文件路径。"""
    session_id = _safe_path_segment(tool_context.session_id if tool_context is not None else "run")
    call_id = _safe_path_segment(tool_context.call_id if tool_context is not None else "call")
    root = workspace_root / ".kongming" / "map_reduce_inline_inputs" / session_id / call_id
    root.mkdir(parents=True, exist_ok=True)
    rel_files: list[str] = []
    objective_text = str(objective).strip() if objective is not None else ""
    for index in range(1, count + 1):
        path = root / f"inline-{index:03d}.txt"
        path.write_text(
            "\n".join(
                [
                    f"inline_shard: {index}",
                    f"total_inline_shards: {count}",
                    f"objective: {objective_text}",
                    "note: this file is a synthetic map_reduce input placeholder.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        rel_files.append(path.relative_to(root).as_posix())
    return root.relative_to(workspace_root).as_posix(), rel_files


def _safe_path_segment(value: str) -> str:
    """清理路径段，输入为任意 ID，输出为安全目录名。"""
    cleaned = "".join(char if char.isalnum() or char in "-_" else "-" for char in value)
    return cleaned.strip("-") or "unknown"


def _object_copy(value: Any) -> dict[str, Any] | None:
    """复制对象字段，输入为任意值，输出为 dict 副本或 None。"""
    if isinstance(value, dict):
        return dict(value)
    return None


def _coerce_string_array(value: Any) -> Any:
    """归一化字符串数组，输入为模型常见数组变体，输出为 list 或原值。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict) and "item" in value:
        item = value.get("item")
        if item is None:
            return []
        if isinstance(item, list):
            return item
        if isinstance(item, tuple):
            return list(item)
        if isinstance(item, str) and item.strip().lower() in _NULL_STRINGS:
            return []
        return [item]
    if isinstance(value, str):
        if value.strip().lower() in _NULL_STRINGS:
            return []
        return [value]
    return value


def _coerce_int(value: Any) -> Any:
    """归一化整数字段，输入为整数或数字字符串，输出为 int 或原值。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit() or (stripped.startswith("-") and stripped[1:].isdigit()):
            return int(stripped)
    return value


def _coerce_bool(value: Any) -> Any:
    """归一化布尔字段，输入为 bool 或 true/false 字符串，输出为 bool 或原值。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return value


def _coerce_nullable_string(value: Any) -> Any:
    """归一化可空字符串，输入为 null-like 字符串，输出为 None 或原值。"""
    if isinstance(value, str) and value.strip().lower() in _NULL_STRINGS:
        return None
    return value


def _unwrap_map_reduce_spec_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """解包模型误生成的 MapReduceWorkflowSpec 外层，输入为 payload，输出为规范 payload。"""
    nested = payload.get(_MAP_REDUCE_SPEC_WRAPPER)
    if not isinstance(nested, dict):
        return payload
    has_required_top_level = any(key in payload for key in _MAP_REDUCE_REQUIRED_PAYLOAD_KEYS)
    if has_required_top_level:
        return payload
    unwrapped = dict(nested)
    if isinstance(payload.get("desc"), str) and "desc" not in unwrapped:
        unwrapped["desc"] = payload["desc"]
    return unwrapped


def _unwrap_roundtable_review_spec_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """解包模型误生成的 RoundtableReviewSpec 外层，输入为 payload，输出为规范 payload。"""
    nested = payload.get(_ROUNDTABLE_REVIEW_SPEC_WRAPPER)
    if not isinstance(nested, dict):
        return payload
    if any(key in payload for key in ("topic", "input_source", "module_path")):
        return payload
    unwrapped = dict(nested)
    if isinstance(payload.get("desc"), str) and "desc" not in unwrapped:
        unwrapped["desc"] = payload["desc"]
    return unwrapped


def _unwrap_deep_research_spec_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """解包模型误生成的 DeepResearchSpec 外层，输入为 payload，输出为规范 payload。"""
    nested = payload.get(_DEEP_RESEARCH_SPEC_WRAPPER)
    if not isinstance(nested, dict):
        return payload
    if any(key in payload for key in ("topic", "source_queries", "source_fixture")):
        return payload
    unwrapped = dict(nested)
    if isinstance(payload.get("desc"), str) and "desc" not in unwrapped:
        unwrapped["desc"] = payload["desc"]
    return unwrapped


def _format_result(result: Any) -> str:
    lines = [
        f"workflow_id: {result.workflow_id}",
        f"desc: {getattr(result, 'desc', None) or ''}",
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
        roundtable = data.get("roundtable_review")
        if isinstance(roundtable, dict):
            board = roundtable.get("review_board")
            if isinstance(board, dict):
                final_report_path = board.get("final_report_path")
                if isinstance(final_report_path, str):
                    lines.extend(["", f"roundtable_final_report: {final_report_path}"])
        deep_research = data.get("deep_research")
        if isinstance(deep_research, dict):
            artifact_paths = deep_research.get("artifact_paths")
            if isinstance(artifact_paths, dict):
                report_path = artifact_paths.get("report_path")
                if isinstance(report_path, str):
                    lines.extend(["", f"deep_research_report: {report_path}"])
    return "\n".join(lines)


def _result_data(result: Any) -> dict[str, Any]:
    data = {
        "workflow_id": result.workflow_id,
        "mode": result.mode,
        "desc": getattr(result, "desc", None),
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
