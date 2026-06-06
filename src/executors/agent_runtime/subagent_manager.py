"""Sub-agent execution manager.

This module owns child-agent lifecycle for in-process orchestration. It keeps
child sessions independent and only feeds each child the dispatch payload
selected by the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.agent_spec import AgentSpec
from core.contracts import Tool
from core.result import Result

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
    ) -> SubAgentRun:
        """Run one child task in a fresh session."""
        session_id = self._build_child_session_id(
            workflow_id=workflow_id,
            parent_session_id=parent_session_id,
            task_id=_session_task_id(task),
        )
        session = self._runtime.session_factory(session_id)
        agent_spec = self._build_child_agent_spec(task)
        enabled_tools = self._resolve_enabled_tools(task)
        result = await self._runtime.runner.run(
            self._build_dispatch_prompt(task),
            session=session,
            agent_spec=agent_spec,
            llm=self._runtime.llm,
            tools=self._runtime.tools,
            approval=self._runtime.approval,
            max_turns=min(3, self._runtime.config.runner.max_turns),
            enabled_tools=enabled_tools,
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


def _session_task_id(task: SubAgentTask) -> str:
    raw = task.metadata.get("session_task_id")
    if isinstance(raw, str) and raw.strip():
        return raw
    return task.task_id


__all__ = ["SubAgentManager", "SubAgentRun", "SubAgentTask"]
