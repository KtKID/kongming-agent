"""Agent workflow manager.

The workflow manager is the boundary for multi-agent orchestration. Version 1
implements one mode: run N independent child-agent tasks in parallel and return
their reports to the parent agent.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from config_loader.models import Config
from executors.agent_runtime.subagent_manager import SubAgentManager, SubAgentRun, SubAgentTask

WorkflowMode = Literal["parallel"]


@dataclass(frozen=True)
class AgentWorkflowResult:
    """Final workflow result returned to the caller."""

    workflow_id: str
    mode: WorkflowMode
    parent_session_id: str
    workflow_dir: Path
    started_at: str
    finished_at: str
    runs: tuple[SubAgentRun, ...]

    @property
    def completed(self) -> bool:
        return all(run.status == "completed" for run in self.runs)


class AgentWorkflowManager:
    """Coordinates sub-agents and owns workflow audit files."""

    def __init__(
        self,
        *,
        subagents: SubAgentManager,
        config: Config,
        workspace_root: Path,
    ) -> None:
        self._subagents = subagents
        self._config = config
        self._workspace_root = workspace_root.resolve()

    async def run_parallel(
        self,
        *,
        parent_session_id: str,
        tasks: list[SubAgentTask],
    ) -> AgentWorkflowResult:
        """Run all tasks concurrently with independent child sessions."""
        if not tasks:
            raise ValueError("parallel workflow requires at least one task")

        workflow_id = f"wf-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        started_at = _now_iso()
        workflow_dir = self._workflow_dir(
            parent_session_id=parent_session_id, workflow_id=workflow_id
        )
        agents_dir = workflow_dir / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)

        assigned_tasks = [
            self._with_agent_workdir(task, agents_dir / _slug(task.task_id)) for task in tasks
        ]
        self._write_workflow_manifest(
            workflow_dir,
            workflow_id=workflow_id,
            mode="parallel",
            parent_session_id=parent_session_id,
            started_at=started_at,
            tasks=assigned_tasks,
            status="running",
        )
        self._append_audit(
            workflow_dir,
            action="workflow_started",
            payload={
                "workflow_id": workflow_id,
                "mode": "parallel",
                "parent_session_id": parent_session_id,
                "task_count": len(assigned_tasks),
            },
        )
        for task in assigned_tasks:
            self._append_audit(
                workflow_dir,
                action="agent_assigned",
                payload=_task_payload(task),
            )

        runs = await asyncio.gather(
            *[
                self._run_one(
                    workflow_id=workflow_id,
                    parent_session_id=parent_session_id,
                    workflow_dir=workflow_dir,
                    task=task,
                )
                for task in assigned_tasks
            ]
        )
        finished_at = _now_iso()
        result = AgentWorkflowResult(
            workflow_id=workflow_id,
            mode="parallel",
            parent_session_id=parent_session_id,
            workflow_dir=workflow_dir,
            started_at=started_at,
            finished_at=finished_at,
            runs=tuple(runs),
        )
        self._append_audit(
            workflow_dir,
            action="workflow_completed",
            payload={
                "workflow_id": workflow_id,
                "completed": result.completed,
                "finished_at": finished_at,
                "run_count": len(runs),
            },
        )
        self._write_workflow_manifest(
            workflow_dir,
            workflow_id=workflow_id,
            mode="parallel",
            parent_session_id=parent_session_id,
            started_at=started_at,
            tasks=assigned_tasks,
            status="completed" if result.completed else "failed",
            finished_at=finished_at,
        )
        self._write_json(
            workflow_dir / "result.json",
            {
                "workflow_id": workflow_id,
                "mode": "parallel",
                "parent_session_id": parent_session_id,
                "workflow_dir": str(workflow_dir),
                "started_at": started_at,
                "finished_at": finished_at,
                "completed": result.completed,
                "runs": [_run_payload(run) for run in runs],
            },
        )
        return result

    async def run_parallel_specs(
        self,
        *,
        parent_session_id: str,
        task_specs: list[dict[str, object]],
    ) -> AgentWorkflowResult:
        """Parse public task specs and run the v1 parallel workflow."""
        tasks: list[SubAgentTask] = []
        for index, spec in enumerate(task_specs, 1):
            raw_tool_names = spec.get("tool_names", [])
            if not isinstance(raw_tool_names, list | tuple):
                raw_tool_names = []
            tasks.append(
                SubAgentTask(
                    task_id=f"agent-{index}",
                    task_name=str(spec["task_name"]),
                    prompt=str(spec["prompt"]),
                    context=str(spec.get("context", "")),
                    tool_names=tuple(str(name) for name in raw_tool_names),
                )
            )
        return await self.run_parallel(parent_session_id=parent_session_id, tasks=tasks)

    async def _run_one(
        self,
        *,
        workflow_id: str,
        parent_session_id: str,
        workflow_dir: Path,
        task: SubAgentTask,
    ) -> SubAgentRun:
        started = time.perf_counter()
        run = await self._subagents.run_task(
            workflow_id=workflow_id,
            parent_session_id=parent_session_id,
            task=task,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        payload = {**_run_payload(run), "elapsed_ms": elapsed_ms}
        self._append_audit(workflow_dir, action="agent_completed", payload=payload)
        self._write_json(workflow_dir / "agents" / _slug(task.task_id) / "result.json", payload)
        return run

    def _with_agent_workdir(self, task: SubAgentTask, workdir: Path) -> SubAgentTask:
        workdir.mkdir(parents=True, exist_ok=True)
        metadata = dict(task.metadata)
        metadata["working_dir"] = str(workdir)
        return SubAgentTask(
            task_id=task.task_id,
            task_name=task.task_name,
            prompt=task.prompt,
            context=task.context,
            tool_names=task.tool_names,
            metadata=metadata,
        )

    def _workflow_dir(self, *, parent_session_id: str, workflow_id: str) -> Path:
        sessions_root = Path(self._config.session.file_store_path)
        if not sessions_root.is_absolute():
            sessions_root = self._workspace_root / sessions_root
        sessions_root = sessions_root.resolve()
        if not _is_relative_to(sessions_root, self._workspace_root):
            raise ValueError(
                "agent workflow audit root must stay inside workspace: "
                f"{sessions_root} is outside {self._workspace_root}"
            )
        return sessions_root / parent_session_id / "agent-workflows" / workflow_id

    def _write_workflow_manifest(
        self,
        workflow_dir: Path,
        *,
        workflow_id: str,
        mode: WorkflowMode,
        parent_session_id: str,
        started_at: str,
        tasks: list[SubAgentTask],
        status: str,
        finished_at: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "workflow_id": workflow_id,
            "mode": mode,
            "parent_session_id": parent_session_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "status": status,
            "assigned_agents": [_task_payload(task) for task in tasks],
        }
        self._write_json(workflow_dir / "workflow.json", payload)

    def _append_audit(self, workflow_dir: Path, *, action: str, payload: dict[str, object]) -> None:
        record = {
            "ts": _now_iso(),
            "action": action,
            "payload": payload,
        }
        workflow_dir.mkdir(parents=True, exist_ok=True)
        with open(workflow_dir / "audit.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _write_json(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(path)


def _task_payload(task: SubAgentTask) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "task_name": task.task_name,
        "tool_names": list(task.tool_names),
        "working_dir": task.metadata.get("working_dir"),
    }


def _run_payload(run: SubAgentRun) -> dict[str, object]:
    return {
        "task_id": run.task.task_id,
        "task_name": run.task.task_name,
        "session_id": run.session_id,
        "run_id": run.run_id,
        "status": run.status,
        "content": run.content,
        "error_message": run.error_message,
        "turn_count": run.turn_count,
        "working_dir": run.task.metadata.get("working_dir"),
    }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _slug(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value.strip())
    safe = safe.strip("-_").lower()
    return safe or "task"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


__all__ = ["AgentWorkflowManager", "AgentWorkflowResult", "WorkflowMode"]
