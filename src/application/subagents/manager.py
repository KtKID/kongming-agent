"""Sub-agent execution manager.

This module owns child-agent lifecycle for in-process orchestration. It keeps
child sessions independent and only feeds each child the dispatch payload
selected by the caller.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from application.subagents.lifecycle import (
    SubAgentLifecycleRegistry,
    get_default_subagent_lifecycle_registry,
)
from application.subagents.permissions import (
    SubAgentApprovalProvider,
    SubAgentCreationRecord,
    SubAgentGrant,
    SubAgentPermissionSpec,
    SubAgentToolAuditHook,
    WorkflowAuditWriter,
    validate_scoped_tool_names,
    wrap_scoped_file_tools,
)
from application.subagents.runtime_resolver import (
    ResolvedSubAgentRuntime,
    resolved_runtime_payload,
)
from core.agent_spec import AgentSpec, coerce_reasoning_effort
from core.contracts import Tool
from core.result import Result
from infrastructure.config.paths import resolve_kongming_path

if TYPE_CHECKING:
    from runtime_assembly.native_runtime import NativeRuntime

_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")
_SubAgentFinishedStatus = Literal["completed", "failed", "cancelled"]


@dataclass(frozen=True)
class SubAgentTask:
    """A single child-agent task."""

    task_id: str
    task_name: str
    prompt: str
    context: str = ""
    tool_names: tuple[str, ...] = ()
    skill_names: tuple[str, ...] = ()
    agent_role_id: str | None = None
    permission: SubAgentPermissionSpec | None = None
    runtime: ResolvedSubAgentRuntime | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class AvailableToolDescription:
    """子 agent prompt 可见工具说明。"""

    name: str
    description: str


@dataclass(frozen=True)
class SubAgentPromptInput:
    """传给子 agent LLM 的独立 prompt 数据结构。"""

    task_name: str
    task_description: str
    context: str
    available_tools: tuple[AvailableToolDescription, ...] = ()
    working_dir_hint: str | None = None


@dataclass(frozen=True)
class SubAgentRun:
    """Completed child-agent run record."""

    task: SubAgentTask
    session_id: str
    run_id: str
    status: str
    content: str
    error_message: str | None
    turn_count: int
    usage: dict[str, int] = field(default_factory=dict)


class SubAgentManager:
    """Runs isolated child agents through the shared Runner boundary.

    .. deprecated:: agent-tree-v0.1 task-5

        本类已**标记 DEPRECATED**（agent-tree-v0.1 task-5）。spawn 主路径已迁移到
        :class:`application.agents.manager.AgentManager`（异步 spawn + 树级打断 +
        ``single_shot`` AgentCell）。

        **保留原因**：workflow strategies（parallel / map_reduce / roundtable_review /
        task_flow / deep_research 共 6 文件）深度依赖 :class:`SubAgentTask` 作 task
        spec 载体，spec 同时要求「workflow 不破坏」。完整淘汰 SubAgentManager 全家桶
        推迟到「workflow 收编为 policy agent」（v2）。本 task（task-5）仅新增
        AgentManager 并存，不删本类。

        **新代码不要使用本类**——派生子 agent 走
        :class:`application.agents.manager.AgentManager.spawn`。
    """

    def __init__(
        self,
        parent_runtime: NativeRuntime,
        *,
        lifecycle_registry: SubAgentLifecycleRegistry | None = None,
    ) -> None:
        self._runtime = parent_runtime
        self._lifecycle_registry = lifecycle_registry or get_default_subagent_lifecycle_registry()

    async def run_task(
        self,
        *,
        workflow_id: str | None = None,
        parent_session_id: str,
        task: SubAgentTask,
        audit_writer: WorkflowAuditWriter | None = None,
        source: str = "workflow",
    ) -> SubAgentRun:
        """Run one child task in a fresh session."""
        runtime = _require_runtime(task)
        resolved_workflow_id = workflow_id or source or "subagent"
        task_run_id = _task_run_id(task)
        session_id = self._build_child_session_id(
            workflow_id=resolved_workflow_id,
            parent_session_id=parent_session_id,
            task_id=task_run_id,
        )
        self._lifecycle_registry.record_started(
            thread_id=parent_session_id,
            source=source,
            workflow_id=workflow_id,
            task_id=task.task_id,
            task_run_id=task_run_id,
            task_name=task.task_name,
            session_id=session_id,
        )
        try:
            session = self._runtime.session_factory(session_id)
            agent_spec = self._build_child_agent_spec(task)
            enabled_tools = self._resolve_enabled_tools(task)
            approval = self._runtime.approval
            lifecycle_hooks = []
            if task.permission is not None:
                if audit_writer is None:
                    raise ValueError("scoped subagent task requires audit writer")
                validate_scoped_tool_names(task.tool_names)
                grant = self._create_grant(
                    workflow_id=resolved_workflow_id,
                    parent_session_id=parent_session_id,
                    session_id=session_id,
                    task=task,
                )
                creation_record = self._create_creation_record(
                    grant=grant,
                    task=task,
                    audit_writer=audit_writer,
                )
                audit_writer.write_subagent_creation(creation_record)
                enabled_tools = wrap_scoped_file_tools(enabled_tools, grant)
                approval = SubAgentApprovalProvider(
                    grant=grant,
                    audit_writer=audit_writer,
                    upstream=self._runtime.approval,
                )
                lifecycle_hooks.append(
                    SubAgentToolAuditHook(grant=grant, audit_writer=audit_writer)
                )
            result = await self._runtime.runner.run(
                self._build_dispatch_prompt(task),
                session=session,
                agent_spec=agent_spec,
                llm=self._runtime.llm,
                tools=self._runtime.tools,
                approval=approval,
                max_turns=runtime.max_turns,
                enabled_tools=enabled_tools,
                lifecycle_hooks=lifecycle_hooks,
                max_tokens=runtime.max_tokens,
                temperature=runtime.temperature,
                timeout_seconds=runtime.timeout_seconds,
                llm_request_metadata=_llm_request_metadata(runtime),
            )
        except asyncio.CancelledError as exc:
            self._record_finished(
                workflow_id=workflow_id,
                parent_session_id=parent_session_id,
                source=source,
                task=task,
                task_run_id=task_run_id,
                session_id=session_id,
                status="cancelled",
                error_message=str(exc) or "cancelled",
            )
            raise
        except Exception as exc:
            self._record_finished(
                workflow_id=workflow_id,
                parent_session_id=parent_session_id,
                source=source,
                task=task,
                task_run_id=task_run_id,
                session_id=session_id,
                status="failed",
                error_message=str(exc) or type(exc).__name__,
            )
            raise
        run = self._to_run(task=task, session_id=session_id, result=result)
        self._record_finished(
            workflow_id=workflow_id,
            parent_session_id=parent_session_id,
            source=source,
            task=task,
            task_run_id=task_run_id,
            session_id=session_id,
            status=_finished_status(run.status),
            error_message=run.error_message,
        )
        return run

    def _build_child_agent_spec(self, task: SubAgentTask) -> AgentSpec:
        runtime = _require_runtime(task)
        role_instructions = ""
        if runtime.role_description:
            role_instructions = f"你的角色职责：{runtime.role_description}。"
        return AgentSpec(
            name=f"subagent-{_slug(task.task_name)}",
            instructions=(
                "你是 kongming 子 agent。只处理分派给你的任务。"
                "只使用本次派发的任务文本和必要上下文。"
                "如果任务给出工作目录，文件写入必须位于该目录内。"
                "任务要求输出结论或报告时，直接作为最终回复返回。"
                "只有任务明确要求写文件且提供写入工具时才写文件。"
                "输出包含：结论、关键依据、风险或未完成项。"
                f"{role_instructions}"
            ),
            default_model=runtime.model,
            tool_names=tuple(task.tool_names),
            max_turns=runtime.max_turns,
            metadata={
                "agent_role": "subagent",
                "subagent_model_name": runtime.model,
                "subagent_role_id": runtime.role_id or "",
            },
            reasoning_effort=coerce_reasoning_effort(runtime.reasoning_effort),
        )

    def _build_dispatch_prompt(self, task: SubAgentTask) -> str:
        prompt_input = SubAgentPromptInput(
            task_name=task.task_name,
            task_description=task.prompt.strip(),
            context=task.context.strip(),
            available_tools=tuple(
                AvailableToolDescription(name=tool.name, description=tool.description)
                for tool in self._resolve_enabled_tools(task)
            ),
            working_dir_hint=_working_dir_hint(task),
        )
        return _render_prompt_input(prompt_input)

    def _build_dispatch_prompt_legacy(self, task: SubAgentTask) -> str:
        parts = [
            f"任务名称：{task.task_name}",
            "",
            "任务：",
            task.prompt.strip(),
        ]
        if task.context.strip():
            parts.extend(["", "必要上下文：", task.context.strip()])
        working_dir = task.metadata.get("working_dir")
        if isinstance(working_dir, str) and working_dir.strip():
            parts.extend(["", "工作目录：", working_dir.strip()])
            if task.tool_names:
                parts.append("所有文件写入都必须在这个工作目录内。")
        return "\n".join(parts).strip()

    def _resolve_enabled_tools(self, task: SubAgentTask) -> list[Tool]:
        resolved: list[Tool] = []
        for name in task.tool_names:
            if name not in self._runtime.tools:
                raise ValueError(f"subagent task {task.task_id!r} requested unknown tool {name!r}")
            resolved.append(self._runtime.tools[name])
        return resolved

    def _create_grant(
        self,
        *,
        workflow_id: str,
        parent_session_id: str,
        session_id: str,
        task: SubAgentTask,
    ) -> SubAgentGrant:
        task_run_id = _task_run_id(task)
        task_run_dir = _metadata_path(task, "task_run_dir")
        working_dir = _metadata_path(task, "working_dir")
        if task_run_dir is None or working_dir is None:
            raise ValueError("scoped subagent task requires task_run_dir and working_dir")
        created_at = _now_iso()
        return SubAgentGrant(
            grant_id=f"grant-{workflow_id}-{task_run_id}",
            workflow_id=workflow_id,
            parent_session_id=parent_session_id,
            task_id=task.task_id,
            task_run_id=task_run_id,
            task_name=task.task_name,
            session_id=session_id,
            task_run_dir=task_run_dir,
            working_dir=working_dir,
            workflow_dir=task_run_dir.parent.parent,
            allowed_tools=frozenset(task.tool_names),
            allowed_skills=frozenset(task.skill_names),
            created_at=created_at,
        )

    def _create_creation_record(
        self,
        *,
        grant: SubAgentGrant,
        task: SubAgentTask,
        audit_writer: WorkflowAuditWriter,
    ) -> SubAgentCreationRecord:
        if task.permission is None:
            raise ValueError("scoped subagent task requires permission")
        return SubAgentCreationRecord(
            version=1,
            workflow_id=grant.workflow_id,
            task_run_id=grant.task_run_id,
            session_id=grant.session_id,
            task_id=task.task_id,
            task_name=task.task_name,
            prompt=task.prompt,
            context=task.context,
            tool_names=task.tool_names,
            skill_names=task.skill_names,
            resolved_runtime=resolved_runtime_payload(task.runtime) or {},
            permission=task.permission,
            grant=grant,
            task_run_dir=grant.task_run_dir,
            working_dir=grant.working_dir,
            child_session_log_path=self._child_session_log_path(grant.session_id),
            workflow_audit_log_path=audit_writer.audit_log_path,
            created_at=grant.created_at,
        )

    def _child_session_log_path(self, session_id: str) -> Path:
        root = resolve_kongming_path(self._runtime.config.session.file_store_path)
        return root / session_id / f"{session_id}.jsonl"

    def _to_run(self, *, task: SubAgentTask, session_id: str, result: Result) -> SubAgentRun:
        content = ""
        if result.final_message is not None and result.final_message.content is not None:
            content = result.final_message.content
        error_message = result.error.message if result.error is not None else None
        return SubAgentRun(
            task=task,
            session_id=session_id,
            run_id=result.run_id,
            status=result.status,
            content=content,
            error_message=error_message,
            turn_count=result.turn_count,
            usage=_usage_from_result_metadata(result.metadata),
        )

    def _record_finished(
        self,
        *,
        workflow_id: str | None,
        parent_session_id: str,
        source: str,
        task: SubAgentTask,
        task_run_id: str,
        session_id: str,
        status: _SubAgentFinishedStatus,
        error_message: str | None,
    ) -> None:
        self._lifecycle_registry.record_finished(
            thread_id=parent_session_id,
            source=source,
            workflow_id=workflow_id,
            task_id=task.task_id,
            task_run_id=task_run_id,
            task_name=task.task_name,
            session_id=session_id,
            status=status,
            error_message=error_message,
        )

    def _build_child_session_id(
        self,
        *,
        workflow_id: str,
        parent_session_id: str,
        task_id: str,
    ) -> str:
        parent = _slug(parent_session_id, max_len=32)
        workflow = _slug(workflow_id, max_len=32)
        task = _slug(task_id, max_len=32)
        return f"subagent-{parent}-{workflow}-{task}"


def _slug(value: str, *, max_len: int = 48) -> str:
    slug = _SLUG_RE.sub("-", value.strip()).strip("-").lower()
    if not slug:
        slug = "task"
    return slug[:max_len]


def _task_run_id(task: SubAgentTask) -> str:
    raw = task.metadata.get("task_run_id")
    if isinstance(raw, str) and raw.strip():
        return raw
    return task.task_id


def _metadata_path(task: SubAgentTask, key: str) -> Path | None:
    value = task.metadata.get(key)
    if isinstance(value, str) and value.strip():
        return Path(value).resolve()
    return None


def _task_max_turns(task: SubAgentTask) -> int:
    """解析子任务最大 turn 数，输入为任务 metadata，输出为正整数上限。"""
    raw = task.metadata.get("max_turns")
    if isinstance(raw, int) and raw > 0:
        return raw
    return 3


def _require_runtime(task: SubAgentTask) -> ResolvedSubAgentRuntime:
    """读取已解析 runtime，输入为任务，输出运行参数或抛错。"""
    if task.runtime is None:
        raise ValueError(f"subagent task {task.task_id!r} has no resolved runtime")
    return task.runtime


def _finished_status(status: str) -> _SubAgentFinishedStatus:
    if status in {"completed", "failed", "cancelled"}:
        return cast(_SubAgentFinishedStatus, status)
    return "failed"


def _usage_from_result_metadata(metadata: dict[str, object]) -> dict[str, int]:
    raw = metadata.get("usage")
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(key): value
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
    }


def _llm_request_metadata(runtime: ResolvedSubAgentRuntime) -> dict[str, object]:
    return {"resolved_runtime": runtime.to_payload()}


def _working_dir_hint(task: SubAgentTask) -> str | None:
    """生成工作目录提示，输入为任务 metadata，输出 prompt 可见提示或 None。"""
    raw = task.metadata.get("working_dir")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _render_prompt_input(prompt_input: SubAgentPromptInput) -> str:
    """渲染子 agent prompt，输入为 prompt 数据结构，输出最终文本。"""
    parts = [
        f"任务名称：{prompt_input.task_name}",
        "",
        "任务描述：",
        prompt_input.task_description,
    ]
    if prompt_input.context.strip():
        parts.extend(["", "上下文：", prompt_input.context.strip()])
    if prompt_input.available_tools:
        parts.extend(["", "可用工具："])
        for tool in prompt_input.available_tools:
            parts.append(f"- {tool.name}: {tool.description}")
    if prompt_input.working_dir_hint:
        parts.extend(
            [
                "",
                "工作目录提示：",
                prompt_input.working_dir_hint,
                "文件写入限定在这个工作目录内。",
            ]
        )
    return "\n".join(parts).strip()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "AvailableToolDescription",
    "SubAgentManager",
    "SubAgentPromptInput",
    "SubAgentRun",
    "SubAgentTask",
]
