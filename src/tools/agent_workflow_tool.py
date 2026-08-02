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

import json
from importlib import import_module
from pathlib import Path
from typing import Any

from core.contracts import PreparedToolCall, ToolContext, ToolResult
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

_TASK_FLOW_PAYLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "task_flow payload 顶层直接包含 objective、planning、plan、execution。"
        "plan.nodes 是可视化步骤数组；简单任务直接填写，复杂多方案任务先提问再填写。"
    ),
    "properties": {
        "mode": {"type": "string", "enum": ["task_flow"]},
        "objective": {"type": "string"},
        "planning": {
            "type": "object",
            "properties": {
                "interaction_mode": {
                    "type": "string",
                    "enum": ["auto", "guided", "choice_required", "llm_decide"],
                    "default": "llm_decide",
                },
                "choice_policy": {
                    "type": "string",
                    "default": "ask_when_multiple_viable_paths",
                },
            },
        },
        "plan": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "nodes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["title"],
                    },
                },
                "steps": {
                    "type": "array",
                    "description": "nodes 的兼容别名，会归一化为 plan.nodes。",
                },
            },
            "required": ["nodes"],
        },
        "execution": {
            "type": "object",
            "properties": {
                "on_unexpected_severe_issue": {
                    "type": "string",
                    "default": "ask_user",
                },
            },
        },
        "audit_tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["objective", "plan"],
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

    def prepare(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> PreparedToolCall:
        """审批前校验并冻结 parallel task specs、mode 和描述。"""
        self._validate_args(arguments)
        if self._handle.get(context) is None:
            raise RuntimeError("agent workflow manager is not bound")
        if "subagent_runtime" in arguments:
            raise ValueError("subagent_runtime is resolved from agent configuration")
        mode = arguments.get("mode", "parallel")
        if not isinstance(mode, str):
            raise ValueError("'mode' must be a string")
        prepared: dict[str, Any] = {
            "tasks": _parse_tasks(arguments["tasks"]),
            "mode": mode.strip(),
        }
        desc = arguments.get("desc")
        if isinstance(desc, str):
            prepared["desc"] = desc
        return PreparedToolCall(arguments=prepared)

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        manager = self._handle.get(ctx)
        if manager is None:
            raise RuntimeError("agent workflow manager is not bound")

        kwargs: dict[str, Any] = {
            "mode": args["mode"],
            "parent_session_id": ctx.session_id,
            "task_specs": args["tasks"],
            "parent_agent": _parent_agent_from_context(ctx),
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
        "需要具体 payload 字段、示例和风险提示时，先调用 "
        "describe_agent_workflow_strategy(mode=...) 查询。"
    )
    input_schema: dict[str, Any] = {  # noqa: RUF012
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "description": "Workflow orchestration mode registered in AgentWorkflowManager.",
            },
            "payload": {
                "type": "object",
                "description": (
                    "策略参数。parallel 使用 task_specs 或 tasks；map_reduce 的 payload 顶层"
                    "直接使用 MapReduceWorkflowSpec 字段：objective、input_source、"
                    "shard_strategy、mapper、reducer、limits、output_contract。"
                    "roundtable_review 必须使用 participants.select 选择角色，"
                    "不要使用 reviewers。"
                    "task_flow 使用 objective 和 plan.nodes 创建可视化执行计划。"
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
                    **_TASK_FLOW_PAYLOAD_SCHEMA["properties"],
                },
            },
        },
        "required": ["mode", "payload"],
    }

    def __init__(self, handle: AgentWorkflowHandle) -> None:
        self._handle = handle

    def prepare(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> PreparedToolCall:
        """审批前校验 mode/payload 并冻结完整 workflow 规范化结果。"""
        self._validate_args(arguments)
        manager = self._handle.get(context)
        if manager is None:
            raise RuntimeError("agent workflow manager is not bound")
        mode = arguments.get("mode")
        if not isinstance(mode, str) or not mode.strip():
            raise ValueError("'mode' must be a non-empty string")
        payload = arguments.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("'payload' must be an object")
        normalized_mode = mode.strip()
        normalized_payload = _normalize_workflow_payload(
            normalized_mode,
            payload,
            workspace_root=_workflow_workspace_root(manager, context),
            tool_context=context,
        )
        return PreparedToolCall(
            arguments={
                "mode": normalized_mode,
                "payload": normalized_payload,
            }
        )

    async def execute(
        self,
        prepared: PreparedToolCall,
        ctx: ToolContext,
    ) -> ToolResult:
        """执行 workflow 工具，输入为模型参数和上下文，输出为结构化 ToolResult。"""
        args = dict(prepared.arguments)
        mode = args.get("mode") if isinstance(args.get("mode"), str) else None

        try:
            content, data = await self._run(args, ctx)
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
        mode = args["mode"]
        payload = args["payload"]
        if mode == "map_reduce":
            _materialize_planned_inline_map_reduce_input(
                payload,
                workspace_root=_workflow_workspace_root(manager, ctx),
            )
        result = await manager.run_workflow_payload(
            mode=mode,
            parent_session_id=ctx.session_id,
            payload=payload,
            parent_agent=_parent_agent_from_context(ctx),
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
                "4. source_policy 控制 language、freshness_days、allowed_domains、blocked_domains、prefer_primary_sources。\n"
                "   网页搜索统一调用 web_search 工具；底层 MCP 缺失时 web_search 返回工具缺失。\n"
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
                '    "source_policy": {"language": "zh-CN", "freshness_days": null, '
                '"allowed_domains": [], '
                '"blocked_domains": [], "prefer_primary_sources": true},\n'
                '    "output_contract": "deep_research_report"\n'
                "  }\n"
                "}"
            )
        if mode == "task_flow":
            return (
                f"{base}\n\n"
                "run_agent_workflow task_flow 参数修正提示：\n"
                "1. task_flow payload 顶层必须包含 objective 和 plan.nodes。\n"
                "2. 简单任务直接填写 plan.nodes；多方案任务先向用户提问，用户确认后再调用。\n"
                "3. 执行计划时，每完成一个 step 调用 advance_task_progress 的 start/next 命令更新进度。\n"
                "4. 重新调用前先按下面骨架修正参数：\n"
                "{\n"
                '  "mode": "task_flow",\n'
                '  "payload": {\n'
                '    "objective": "完成用户任务目标",\n'
                '    "planning": {"interaction_mode": "llm_decide", '
                '"choice_policy": "ask_when_multiple_viable_paths"},\n'
                '    "plan": {\n'
                '      "title": "任务执行计划",\n'
                '      "nodes": [\n'
                '        {"id": "step-1", "title": "确认目标", '
                '"description": "整理任务边界"}\n'
                "      ]\n"
                "    },\n"
                '    "execution": {"on_unexpected_severe_issue": "ask_user"}\n'
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


class DescribeAgentWorkflowStrategyTool(BaseBuiltinTool):
    """Describe a registered agent workflow strategy."""

    name = "describe_agent_workflow_strategy"
    description = (
        "查询已注册 agent workflow 策略的详细参数说明、适用场景、风险提示、输出和示例。"
        "先用 workflow catalog 选择 mode，再调用本工具生成 run_agent_workflow payload。"
    )
    input_schema: dict[str, Any] = {  # noqa: RUF012
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "description": "Workflow strategy mode, for example map_reduce or task_flow.",
            }
        },
        "required": ["mode"],
    }

    def __init__(self, handle: AgentWorkflowHandle) -> None:
        self._handle = handle

    def prepare(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> PreparedToolCall:
        """审批前校验并冻结 workflow strategy mode。"""
        self._validate_args(arguments)
        if self._handle.get(context) is None:
            raise RuntimeError("agent workflow manager is not bound")
        mode = arguments.get("mode")
        if not isinstance(mode, str) or not mode.strip():
            raise ValueError("'mode' must be a non-empty string")
        return PreparedToolCall(arguments={"mode": mode.strip()})

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        """查询 workflow 策略详情，输入为 mode 和上下文，输出文本说明和结构化数据。"""
        manager = self._handle.get(ctx)
        if manager is None:
            raise RuntimeError("agent workflow manager is not bound")
        description = manager.describe_workflow_strategy(args["mode"])
        data = _workflow_strategy_description_data(description)
        return _format_workflow_strategy_description(data), data


AgentWorkflowTool = RunParallelSubagentsTool


class AgentTreeRuntimeRouter:
    """agent-tree-v0.1 task-5：按 session 路由到 agent tree 运行时 owner。

    装配层在 root agent 运行时 owner 就绪后调 :meth:`bind_dispatcher` 注入；
    工具运行期通过 :meth:`resolve` 取回。``Any`` 类型避免 tools → hosts /
    application 的 import-linter 分层冲突，router 只持有引用并按 duck typing 调用
    ``get_agent`` / ``spawn``。

    生产路径绑定 :class:`hosts.shared.host_dispatcher.HostDispatcher`。它是当前
    session 的 agent tree owner，对 spawn_subagent 暴露 ``get_agent`` 和 ``spawn``。

    per-session 绑定：每个 Web thread 持有独立 HostDispatcher / AgentManager
    （独立 TaskRegistry / epoch / approval_canceller），router 按 ``session_id`` 分桶。
    ``bind_dispatcher(dispatcher)`` 无 session_id 时写默认 dispatcher；``resolve(ctx)``
    优先取当前 session 绑定，其次默认绑定。

    Attributes:
        dispatcher: 默认 agent tree dispatcher；None 表示未绑定。
        _dispatchers_by_session_id: session_id → 该 thread 的 agent tree dispatcher。
    """

    def __init__(self) -> None:
        self.dispatcher: Any | None = None
        self._dispatchers_by_session_id: dict[str, Any] = {}

    def bind_dispatcher(self, dispatcher: Any, *, session_id: str | None = None) -> None:
        """绑定 agent tree dispatcher，输入为 dispatcher，输出为已绑定状态。

        session_id 为空时写默认 dispatcher；非空时写 thread 专属 dispatcher。
        """
        if session_id is None:
            self.dispatcher = dispatcher
            return
        self._dispatchers_by_session_id[session_id] = dispatcher

    def resolve(self, ctx: ToolContext | None = None) -> Any | None:
        """取回 agent tree dispatcher，输入为可选 ToolContext，输出为 dispatcher 或 None。

        优先返回 ctx.session_id 对应的 per-thread 绑定，其次默认绑定；ctx 为 None
        时只取默认绑定。
        """
        if ctx is not None and ctx.session_id:
            per_session = self._dispatchers_by_session_id.get(ctx.session_id)
            if per_session is not None:
                return per_session
        return self.dispatcher


class SpawnSubAgentTool(BaseBuiltinTool):
    """agent-tree-v0.1 task-5：异步派生一个后台子 agent（spawn 主路径）。

    本工具调 ``AgentManager.spawn``，立即返回 ``{child_id, status:"dispatched"}``，
    父永不阻塞；子最终结果走 ``child_result`` Mail 在父下一 run 注入。

    workflow modes 与本入口共享 AgentManager/TaskRegistry 生命周期 owner。
    """

    name = "spawn_subagent"
    description = (
        "Asynchronously spawn a background child agent (single_shot) and return "
        "immediately with {child_id, status:'dispatched'}. The child's final result "
        "is delivered to the parent agent via a child_result message on its next run. "
        "The parent never blocks waiting. Spawn depth is fixed at 1 in v1."
    )
    input_schema: dict[str, Any] = {  # noqa: RUF012
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The seed user message handed to the child agent.",
            },
            "name": {
                "type": "string",
                "description": "Child agent display name.",
            },
            "instructions": {
                "type": "string",
                "description": "Child agent system prompt. Defaults to empty.",
            },
            "tool_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Child agent tool whitelist (default empty = no tools).",
            },
            "model": {
                "type": "string",
                "description": "Child agent default model. Defaults to parent model.",
            },
            "cwd": {
                "type": "string",
                "description": "Child agent working directory.",
            },
        },
        "required": ["prompt", "name", "cwd"],
    }

    def __init__(
        self,
        agent_tree_runtime_router: AgentTreeRuntimeRouter,
        *,
        parent_model: str = "",
    ) -> None:
        self._agent_tree_runtime_router = agent_tree_runtime_router
        self._parent_model = parent_model

    def prepare(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> PreparedToolCall:
        """审批前校验 spawn 参数并冻结默认 instructions/tools/model。"""
        self._validate_args(arguments)
        dispatcher = self._agent_tree_runtime_router.resolve(context)
        if dispatcher is None:
            raise RuntimeError(
                "agent tree runtime is not bound (spawn_subagent requires agent-tree)"
            )
        parent_agent_id = context.agent_id or ""
        parent_cell = dispatcher.get_agent(parent_agent_id)
        if parent_cell is None:
            raise RuntimeError(
                f"parent agent not found for agent_id={parent_agent_id!r}; "
                "spawn_subagent must run within an agent-tree cell"
            )
        prompt = _required_non_empty_string(arguments, "prompt")
        name = _required_non_empty_string(arguments, "name")
        cwd = _required_non_empty_string(arguments, "cwd")
        instructions = arguments.get("instructions", "")
        if not isinstance(instructions, str):
            instructions = ""
        tool_names: list[str] | None = None
        if "tool_names" in arguments:
            raw_tool_names = arguments.get("tool_names")
            if not isinstance(raw_tool_names, list):
                raw_tool_names = []
            tool_names = [name for name in raw_tool_names if isinstance(name, str)]
        model = arguments.get("model")
        if not isinstance(model, str) or not model.strip():
            model = self._parent_model or parent_cell.spec.default_model
        return PreparedToolCall(
            arguments={
                "prompt": prompt,
                "name": name,
                "cwd": cwd,
                "instructions": instructions,
                "tool_names": tool_names,
                "model": model,
            }
        )

    async def _run(
        self,
        args: dict[str, Any],
        ctx: ToolContext,
    ) -> tuple[str, dict[str, Any] | None]:
        """执行 spawn，输入为模型参数 + 上下文，输出为 dispatched 结果文本 + data。"""
        dispatcher = self._agent_tree_runtime_router.resolve(ctx)
        if dispatcher is None:
            raise RuntimeError(
                "agent tree runtime is not bound (spawn_subagent requires agent-tree)"
            )

        prompt = args["prompt"]
        name = args["name"]
        cwd = args["cwd"]

        # 从 ToolContext.agent_id 查父 cell，再用父 cell id 构造统一 spawn request。
        parent_agent_id = ctx.agent_id or ""
        parent_cell = dispatcher.get_agent(parent_agent_id)
        if parent_cell is None:
            raise RuntimeError(
                f"parent agent not found for agent_id={parent_agent_id!r}; "
                "spawn_subagent must run within an agent-tree cell"
            )

        instructions = args["instructions"]
        tool_names_raw = args["tool_names"]
        tool_names = tuple(tool_names_raw) if isinstance(tool_names_raw, list) else None
        model = args["model"]
        spawn_tools: Any = import_module("application.agents.subagent_tools")
        request = spawn_tools.build_spawn_request_from_tool_args(
            parent_agent_id=parent_cell.agent_id,
            source_task_id=ctx.call_id,
            prompt=prompt,
            name=name,
            instructions=instructions,
            tool_names=tool_names,
            cwd=cwd,
            default_model=model,
            max_turns=parent_cell.spec.max_turns,
            metadata={
                "run_id": ctx.run_id,
                "session_id": ctx.session_id,
                "turn": ctx.turn,
            },
        )

        try:
            result = dispatcher.spawn(request)
        except Exception as exc:
            # spawn 拒绝（深度超限 / registry 关门）：返回拒绝 tool_result 不打断父 run。
            error_message = f"spawn rejected: {exc}"
            return error_message, {
                "child_id": None,
                "status": "rejected",
                "error": error_message,
            }

        content = (
            f"child dispatched: agent_id={result.child_id} "
            f"status=dispatched task_id={result.task_id}"
        )
        return content, {
            "child_id": result.child_id,
            "status": result.status,
            "task_id": result.task_id,
        }


def build_agent_workflow_tool(handle: AgentWorkflowHandle) -> RunParallelSubagentsTool:
    return RunParallelSubagentsTool(handle)


def build_run_agent_workflow_tool(handle: AgentWorkflowHandle) -> RunAgentWorkflowTool:
    return RunAgentWorkflowTool(handle)


def build_describe_agent_workflow_strategy_tool(
    handle: AgentWorkflowHandle,
) -> DescribeAgentWorkflowStrategyTool:
    return DescribeAgentWorkflowStrategyTool(handle)


def build_spawn_subagent_tool(
    agent_tree_runtime_router: AgentTreeRuntimeRouter,
    *,
    parent_model: str = "",
) -> SpawnSubAgentTool:
    """构造 spawn_subagent 工具，输入为 agent tree runtime router，输出为工具实例。

    装配层（run.py）在 HostDispatcher 就绪后构造本工具注册进 ToolRegistry。
    ``parent_model`` 透传给子 AgentSpec 的默认模型（子未指定 model 时兜底）。
    """
    return SpawnSubAgentTool(agent_tree_runtime_router, parent_model=parent_model)


def _required_non_empty_string(arguments: dict[str, Any], key: str) -> str:
    """读取必填非空字符串，输入参数和字段名，输出去首尾空白的文本。"""
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key!r} must be a non-empty string")
    return value.strip()


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
    if _has_subagent_runtime_payload(payload):
        raise ValueError("subagent_runtime is resolved from agent configuration")
    if mode == "parallel":
        raw_tasks = payload.get("task_specs", payload.get("tasks"))
        normalized: dict[str, object] = {
            "task_specs": _parse_tasks(raw_tasks),
        }
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
    if mode == "task_flow":
        payload = _normalize_task_flow_payload(payload)
    normalized = dict(payload)
    normalized.setdefault("mode", mode)
    return normalized


def _has_subagent_runtime_payload(payload: dict[str, Any]) -> bool:
    """检查旧 runtime 字段，输入为原始 payload，输出是否出现禁用字段。"""
    if "subagent_runtime" in payload:
        return True
    for wrapper_name in (
        _MAP_REDUCE_SPEC_WRAPPER,
        _ROUNDTABLE_REVIEW_SPEC_WRAPPER,
        _DEEP_RESEARCH_SPEC_WRAPPER,
    ):
        nested = payload.get(wrapper_name)
        if isinstance(nested, dict) and "subagent_runtime" in nested:
            return True
    return False


def _parent_agent_from_context(ctx: ToolContext) -> dict[str, object] | None:
    """读取父 agent 快照，输入为 ToolContext，输出 metadata 中的 parent_agent。"""
    raw = ctx.metadata.get("parent_agent")
    if isinstance(raw, dict):
        snapshot = {str(key): value for key, value in raw.items()}
        if ctx.agent_id:
            snapshot["agent_id"] = ctx.agent_id
        return snapshot
    return None


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

    _plan_inline_map_reduce_input(
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


def _normalize_task_flow_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """归一化 task_flow 参数，输入为模型生成 payload，输出为 parser 友好 payload。"""
    normalized = dict(payload)
    objective = normalized.get("objective", normalized.get("goal"))
    if isinstance(objective, str):
        normalized["objective"] = objective.strip()

    plan = _object_copy(normalized.get("plan"))
    if plan is None:
        plan = {}
        if "nodes" in normalized:
            plan["nodes"] = normalized.get("nodes")
        if "steps" in normalized:
            plan["steps"] = normalized.get("steps")
    if "nodes" not in plan and "steps" in plan:
        plan["nodes"] = plan.get("steps")
    if "nodes" in plan and isinstance(plan["nodes"], tuple):
        plan["nodes"] = list(plan["nodes"])
    normalized["plan"] = plan

    planning = _object_copy(normalized.get("planning")) or {}
    planning.setdefault("interaction_mode", "llm_decide")
    planning.setdefault("choice_policy", "ask_when_multiple_viable_paths")
    normalized["planning"] = planning

    execution = _object_copy(normalized.get("execution")) or {}
    execution.setdefault("on_unexpected_severe_issue", "ask_user")
    normalized["execution"] = execution

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


def _plan_inline_map_reduce_input(
    normalized: dict[str, Any],
    *,
    workspace_root: Path,
    tool_context: ToolContext | None,
) -> None:
    """审批前把 inline 输入转换为确定路径计划，保持文件系统零写入。"""
    input_source = normalized.get("input_source")
    if not isinstance(input_source, dict):
        return
    files = input_source.get("files")
    if not _contains_inline_input(files):
        return
    shard_strategy = normalized.get("shard_strategy")
    count = _inline_shard_count(files, shard_strategy)
    inline_root, rel_files = _inline_map_reduce_paths(
        workspace_root=workspace_root,
        tool_context=tool_context,
        count=count,
    )
    input_source["kind"] = "file_list"
    input_source["root_dir"] = inline_root
    input_source["include"] = []
    input_source["exclude"] = []
    input_source["files"] = rel_files
    input_source["index_provider"] = input_source.get("index_provider") or "inline"
    normalized["output_contract"] = "raw_text"


def _materialize_planned_inline_map_reduce_input(
    normalized: dict[str, Any],
    *,
    workspace_root: Path,
) -> None:
    """审批后按已批准的 root/files 计划写入 inline 占位文件。"""
    input_source = normalized.get("input_source")
    if not isinstance(input_source, dict) or input_source.get("index_provider") != "inline":
        return
    root_dir = input_source.get("root_dir")
    files = input_source.get("files")
    if not isinstance(root_dir, str) or not isinstance(files, list):
        raise ValueError("prepared inline input plan is invalid")
    root = (workspace_root / root_dir).resolve()
    root.relative_to(workspace_root.resolve())
    root.mkdir(parents=True, exist_ok=True)
    objective_text = str(normalized.get("objective", "")).strip()
    total = len(files)
    for index, relative_path in enumerate(files, 1):
        if not isinstance(relative_path, str):
            raise ValueError("prepared inline input file must be a string")
        path = (root / relative_path).resolve()
        path.relative_to(root)
        path.write_text(
            "\n".join(
                [
                    f"inline_shard: {index}",
                    f"total_inline_shards: {total}",
                    f"objective: {objective_text}",
                    "note: this file is a synthetic map_reduce input placeholder.",
                    "",
                ]
            ),
            encoding="utf-8",
        )


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
    if any(value.startswith(root) for root in _TEMP_INLINE_INPUT_ROOTS):
        return not Path(value).expanduser().exists()
    path = Path(value).expanduser()
    if not path.is_absolute() or path.exists():
        return False
    candidates = {path.as_posix(), path.resolve(strict=False).as_posix()}
    return any(
        candidate.startswith(root) for candidate in candidates for root in _TEMP_INLINE_INPUT_ROOTS
    )


def _inline_map_reduce_paths(
    *,
    workspace_root: Path,
    tool_context: ToolContext | None,
    count: int,
) -> tuple[str, list[str]]:
    """规划 inline 输入路径，输入工作区/调用坐标/数量，输出根目录和相对文件。"""
    session_id = _safe_path_segment(tool_context.session_id if tool_context is not None else "run")
    call_id = _safe_path_segment(tool_context.call_id if tool_context is not None else "call")
    root = workspace_root / ".kongming" / "map_reduce_inline_inputs" / session_id / call_id
    rel_files = [f"inline-{index:03d}.txt" for index in range(1, count + 1)]
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
        task_flow = data.get("task_flow")
        if isinstance(task_flow, dict):
            plan_path = task_flow.get("plan_path")
            progress_path = task_flow.get("progress_path")
            if isinstance(plan_path, str):
                lines.extend(["", f"task_flow_plan: {plan_path}"])
            if isinstance(progress_path, str):
                lines.append(f"task_flow_progress: {progress_path}")
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


def _workflow_strategy_description_data(description: Any) -> dict[str, Any]:
    """投影 workflow strategy description，输入为 description 对象，输出 JSON 友好字典。"""
    mode = description.mode
    inputs = [
        {
            "name": item.name,
            "required": item.required,
            "type_label": item.type_label,
            "description": item.description,
            "example": _json_safe_value(
                item.example,
                context=f"workflow strategy {mode} input {item.name} example",
            ),
        }
        for item in description.inputs
    ]
    return {
        "mode": mode,
        "title": description.title,
        "status": description.status,
        "runnable": description.runnable,
        "summary": description.summary,
        "when_to_use": list(description.when_to_use),
        "warnings": list(description.warnings),
        "inputs": inputs,
        "payload_schema": _workflow_payload_schema_from_inputs(inputs),
        "outputs": list(description.outputs),
        "examples": [
            _json_safe_value(
                example,
                context=f"workflow strategy {mode} examples[{index}]",
            )
            for index, example in enumerate(description.examples)
        ],
        "depends_on": list(description.depends_on),
    }


def _format_workflow_strategy_description(data: dict[str, Any]) -> str:
    """格式化 workflow strategy description，输入为数据字典，输出模型可读文本。"""
    lines = [
        f"workflow_strategy: {data['mode']}",
        f"title: {data['title']}",
        f"status: {data['status']}",
        f"runnable: {data['runnable']}",
        f"summary: {data['summary']}",
        "",
        "when_to_use:",
    ]
    lines.extend(f"- {item}" for item in data["when_to_use"])
    if data["warnings"]:
        lines.append("")
        lines.append("warnings:")
        lines.extend(f"- {item}" for item in data["warnings"])
    if data["inputs"]:
        lines.append("")
        lines.append("inputs:")
        for item in data["inputs"]:
            required = "required" if item["required"] else "optional"
            lines.append(
                f"- {item['name']} ({required}, {item['type_label']}): {item['description']}"
            )
            if item["example"] is not None:
                example = json.dumps(item["example"], ensure_ascii=False, sort_keys=True)
                lines.append(f"  example: {example}")
    if data["payload_schema"]:
        schema = json.dumps(
            data["payload_schema"],
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        lines.append("")
        lines.append("payload_schema:")
        lines.append(schema)
    if data["outputs"]:
        lines.append("")
        lines.append("outputs:")
        lines.extend(f"- {item}" for item in data["outputs"])
    if data["examples"]:
        lines.append("")
        lines.append("examples:")
        for example in data["examples"]:
            lines.append(json.dumps(example, ensure_ascii=False, sort_keys=True))
    return "\n".join(lines)


def _workflow_payload_schema_from_inputs(
    inputs: list[dict[str, Any]],
) -> dict[str, Any]:
    """从 input 字段说明派生 payload schema，输入为字段列表，输出 JSON-schema 风格对象。"""
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": True,
        "properties": {},
    }
    required: list[str] = []
    for item in inputs:
        name = item["name"]
        if not isinstance(name, str) or not name.strip():
            continue
        path = tuple(part for part in name.split(".") if part)
        if not path:
            continue
        if item["required"] and path[0] not in required:
            required.append(path[0])
        _insert_payload_schema_property(schema, path, item)
    if required:
        schema["required"] = required
    return schema


def _insert_payload_schema_property(
    schema: dict[str, Any],
    path: tuple[str, ...],
    item: dict[str, Any],
) -> None:
    """写入嵌套 payload schema 属性，输入为 schema/path/item，输出原地更新。"""
    current = schema
    for segment in path[:-1]:
        properties = current.setdefault("properties", {})
        child = properties.setdefault(
            segment,
            {
                "type": "object",
                "additionalProperties": True,
                "properties": {},
            },
        )
        current = child
    properties = current.setdefault("properties", {})
    leaf = path[-1]
    properties[leaf] = _payload_schema_property(item)
    if item["required"]:
        required = current.setdefault("required", [])
        if leaf not in required:
            required.append(leaf)


def _payload_schema_property(item: dict[str, Any]) -> dict[str, Any]:
    """生成单个 payload 字段 schema，输入为字段说明，输出 JSON-schema 风格属性。"""
    schema = _schema_type_from_type_label(item["type_label"])
    schema["description"] = item["description"]
    schema["x-type_label"] = item["type_label"]
    if item["example"] is not None:
        schema["examples"] = [item["example"]]
    return schema


def _schema_type_from_type_label(type_label: Any) -> dict[str, Any]:
    """映射字段类型标签，输入为 type_label，输出 JSON-schema 风格类型。"""
    label = str(type_label).strip().lower()
    if label.startswith("array"):
        schema: dict[str, Any] = {"type": "array"}
        if "object" in label:
            schema["items"] = {"type": "object"}
        elif "string" in label:
            schema["items"] = {"type": "string"}
        return schema
    if label in {"object", "dict", "mapping"}:
        return {"type": "object", "additionalProperties": True}
    if label in {"number", "float", "integer", "int"}:
        return {"type": "number"}
    if label in {"boolean", "bool"}:
        return {"type": "boolean"}
    return {"type": "string"}


def _json_safe_value(value: Any, *, context: str) -> Any:
    """校验 JSON 可序列化值，输入为任意值和上下文，输出原值或带定位错误。"""
    if value is None:
        return None
    try:
        json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError as exc:
        raise ValueError(
            f"{context} must be JSON serializable; got {type(value).__name__}"
        ) from exc
    return value


__all__ = [
    "AgentTreeRuntimeRouter",
    "AgentWorkflowHandle",
    "AgentWorkflowTool",
    "DescribeAgentWorkflowStrategyTool",
    "RunAgentWorkflowTool",
    "RunParallelSubagentsTool",
    "SpawnSubAgentTool",
    "build_agent_workflow_tool",
    "build_describe_agent_workflow_strategy_tool",
    "build_run_agent_workflow_tool",
    "build_spawn_subagent_tool",
]
