"""Sub-agent execution manager.

This module owns child-agent lifecycle for in-process orchestration. It keeps
child sessions independent and only feeds each child the dispatch payload
selected by the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from core.agent_spec import AgentSpec
from core.contracts import Tool
from core.result import Result
from executors.agent_runtime.subagent_permissions import (
    SubAgentApprovalProvider,
    SubAgentCreationRecord,
    SubAgentGrant,
    SubAgentPermissionSpec,
    SubAgentToolAuditHook,
    WorkflowAuditWriter,
    validate_scoped_tool_names,
    wrap_scoped_file_tools,
)

if TYPE_CHECKING:
    from executors.agent_runtime.native_runtime import NativeRuntime

_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True)
class SubAgentTask:
    """A single child-agent task."""

    task_id: str
    task_name: str
    prompt: str
    context: str = ""
    tool_names: tuple[str, ...] = ()
    skill_names: tuple[str, ...] = ()
    permission: SubAgentPermissionSpec | None = None
    metadata: dict[str, object] = field(default_factory=dict)


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


class SubAgentManager:
    """Runs isolated child agents through the shared Runner boundary."""

    def __init__(self, parent_runtime: NativeRuntime) -> None:
        self._runtime = parent_runtime

    async def run_task(
        self,
        *,
        workflow_id: str,
        parent_session_id: str,
        task: SubAgentTask,
        audit_writer: WorkflowAuditWriter | None = None,
    ) -> SubAgentRun:
        """Run one child task in a fresh session."""
        session_id = self._build_child_session_id(
            workflow_id=workflow_id,
            parent_session_id=parent_session_id,
            task_id=_task_run_id(task),
        )
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
                workflow_id=workflow_id,
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
            lifecycle_hooks.append(SubAgentToolAuditHook(grant=grant, audit_writer=audit_writer))
        result = await self._runtime.runner.run(
            self._build_dispatch_prompt(task),
            session=session,
            agent_spec=agent_spec,
            llm=self._runtime.llm,
            tools=self._runtime.tools,
            approval=approval,
            max_turns=min(3, self._runtime.config.runner.max_turns),
            enabled_tools=enabled_tools,
            lifecycle_hooks=lifecycle_hooks,
        )
        return self._to_run(task=task, session_id=session_id, result=result)

    def _build_child_agent_spec(self, task: SubAgentTask) -> AgentSpec:
        return AgentSpec(
            name=f"subagent-{_slug(task.task_name)}",
            instructions=(
                "你是 kongming 子 agent。只处理分派给你的任务。"
                "只使用本次派发的任务文本和必要上下文。"
                "如果任务给出工作目录，文件写入必须位于该目录内。"
                "输出包含：结论、关键依据、风险或未完成项。"
            ),
            default_model=self._runtime.agent_spec.default_model,
            tool_names=tuple(task.tool_names),
            max_turns=3,
            metadata={"agent_role": "subagent"},
            reasoning_effort=self._runtime.agent_spec.reasoning_effort,
        )

    def _build_dispatch_prompt(self, task: SubAgentTask) -> str:
        parts = [
            f"任务名称：{task.task_name}",
            f"任务 ID：{task.task_id}",
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
            permission=task.permission,
            grant=grant,
            task_run_dir=grant.task_run_dir,
            working_dir=grant.working_dir,
            child_session_log_path=self._child_session_log_path(grant.session_id),
            workflow_audit_log_path=audit_writer.audit_log_path,
            created_at=grant.created_at,
        )

    def _child_session_log_path(self, session_id: str) -> Path:
        root = Path(self._runtime.config.session.file_store_path)
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


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["SubAgentManager", "SubAgentRun", "SubAgentTask"]
